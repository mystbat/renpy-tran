#!/usr/bin/env python3
"""rpatool — Ren'Py archived (.rpa) pack/extract tool.

Two layouts are supported:

* **RPA-3.0 with obfuscation key** (most commercial games, e.g. header
  ``RPA-3.0 <16-hex offset> <8-hex key>\\n``): the index is a zlib-compressed
  pickle located at the 16-hex offset; both the index bytes and every entry's
  start/end offsets are XOR-decoded with the key. Entry tuple decodes to
  ``(start, end)`` and the member body is ``archive[start:end]``.
* **Legacy trailer format** (archives written by this module's ``create``,
  used by the test fixtures): ``magic + 4-byte indexlen placeholder`` header,
  a ``[indexlen:4][zlib pickle]`` trailer at EOF, no XOR, entries are
  ``(offset, length)`` pairs resolved via ``ast.literal_eval``.

Based on the public-domain reference tool by Shizmob
(https://github.com/Shizmob/rpatool). Vendored as source per ADR-0005.
"""

from __future__ import annotations

import ast
import os
import pickle
import struct
import sys
import zlib
from pathlib import Path
from typing import Callable, Dict, List, Tuple

__all__ = ["RpaError", "extract", "create", "extract_archive"]

RPA_MAGIC = b"RPA-3.0 "


class RpaError(Exception):
    """Raised for malformed or unsupported .rpa archives."""


def _deobfuscate(data: bytes, key: int) -> bytes:
    """XOR *data* with the 4-byte little-endian *key*, repeating per byte."""
    if not key:
        return data
    k0 = key & 0xFF
    k1 = (key >> 8) & 0xFF
    k2 = (key >> 16) & 0xFF
    k3 = (key >> 24) & 0xFF
    keys = (k0, k1, k2, k3)
    return bytes(b ^ keys[i % 4] for i, b in enumerate(data))


def _read_index_obfuscated(f, header_rest: bytes) -> Dict[str, List[Tuple[int, int]]]:
    """Parse the obfuscated RPA-3.0 layout (offset + optional key in header)."""
    text = header_rest.decode("ascii", errors="replace").strip()
    parts = text.split()
    if len(parts) < 1:
        raise RpaError("RPA-3.0 header missing index offset")
    try:
        index_offset = int(parts[0], 16)
    except ValueError as exc:
        raise RpaError(f"bad index offset in header: {parts[0]!r}") from exc
    key = 0
    if len(parts) >= 2:
        try:
            key = int(parts[1], 16)
        except ValueError as exc:
            raise RpaError(f"bad obfuscation key in header: {parts[1]!r}") from exc

    f.seek(index_offset)
    compressed = f.read()  # to EOF — index sits at file end; cap would truncate
    candidates = (compressed, _deobfuscate(compressed, key))
    index = None
    last_error = None
    for data in candidates:
        try:
            index = pickle.loads(zlib.decompress(data))
            break
        except Exception as exc:  # noqa: BLE001 - try both raw and XOR'd
            last_error = exc
    if index is None:
        raise RpaError(f"failed to decompress/parse RPA index: {last_error}")
    if not isinstance(index, dict):
        raise RpaError("RPA index is not a mapping")

    # Normalise entries to (offset, dlen, prefix) — official loader.py semantics:
    #   2-tuple: index[k] = [(offset ^ key, dlen ^ key), ...]
    #   3-tuple: (offset ^ key, dlen ^ key, start_bytes) — start_bytes are the
    #   file's leading bytes stored *in the index* (dedup optimisation); full
    #   content = start_bytes + archive[offset:offset+dlen].
    decoded: Dict[str, List[Tuple[int, int, bytes]]] = {}
    for name, entries in index.items():
        spans: List[Tuple[int, int, bytes]] = []
        for entry in entries:
            if key:
                ints = [v ^ key for v in entry if isinstance(v, int)]
                if len(ints) >= 2:
                    off, dlen = ints[0], ints[1]
                else:
                    continue
            else:
                off, dlen = entry[0], entry[1]
            prefix = b""
            if len(entry) >= 3:
                s = entry[2]
                if isinstance(s, bytes):
                    prefix = s
                elif s:
                    prefix = s.encode("latin-1")
            spans.append((off, dlen, prefix))
        decoded[name] = spans
    return decoded


def _read_index_legacy_trailer(f) -> Dict[str, List[Tuple[int, int]]]:
    """Parse the trailer layout written by ``create`` (no XOR, repr pickle)."""
    f.seek(0, os.SEEK_END)
    size = f.tell()
    f.seek(len(RPA_MAGIC))
    (tmpkey,) = struct.unpack("i", f.read(4))
    f.seek(size - (4 + tmpkey))
    (indexlen,) = struct.unpack("i", f.read(4))
    if indexlen != tmpkey:
        raise RpaError(
            f"RPA trailer inconsistent: indexlen={indexlen}, data={tmpkey}"
        )
    data = f.read(tmpkey)
    try:
        decompressed = zlib.decompress(data)
    except zlib.error as exc:
        raise RpaError(f"failed to decompress RPA index: {exc}") from exc
    try:
        index = ast.literal_eval(decompressed.decode("utf-8"))
    except (ValueError, SyntaxError) as exc:
        raise RpaError(f"failed to parse RPA index: {exc}") from exc
    if not isinstance(index, dict):
        raise RpaError("RPA index is not a mapping")
    return index


def _read_index(f) -> Dict[str, List[Tuple[int, int]]]:
    """Dispatch between the obfuscated header layout and the legacy trailer."""
    f.seek(0)
    header = f.read(48)
    if header[:8] != RPA_MAGIC:
        raise RpaError(f"unsupported archive magic: {header[:8]!r}")
    header_rest = header[8:]
    stripped = header_rest.lstrip(b"0123456789abcdefABCDEF")
    # Obfuscated layout: hex offset (+ optional hex key) directly after magic,
    # followed by a newline and the "Made with Ren'py" banner.
    if header_rest[:1] in b"0123456789abcdefABCDEF" and b"\n" in header_rest:
        return _read_index_obfuscated(f, header_rest.split(b"\n")[0])
    return _read_index_legacy_trailer(f)


def extract_archive(
    rpa_path: Path,
    out_dir: Path,
    progress: "Callable[[str, int, int], None] | None" = None,
) -> List[Path]:
    """Extract every member of ``rpa_path`` into ``out_dir``.

    ``progress(name, done, total)`` is invoked per member for UI progress
    display. Archived names use ``/`` separators; these are mapped to the OS
    separator and parent directories are created as needed. Returns the list
    of written file paths (top-level entries only, matching
    ``Unren.extract_rpa``).
    """
    rpa_path = Path(rpa_path)
    out_dir = Path(out_dir)
    if not rpa_path.is_file():
        raise RpaError(f"archive not found: {rpa_path}")

    with rpa_path.open("rb") as f:
        index = _read_index(f)

        top_level: List[Path] = []
        seen_top: set[str] = set()
        total = len(index)
        for done, (name, parts) in enumerate(index.items(), 1):
            if progress is not None and (done % 50 == 0 or done == total):
                progress(name, done, total)
            rel = name.replace("/", os.sep)
            dest = out_dir / rel
            top = rel.split(os.sep, 1)[0]
            if top not in seen_top:
                seen_top.add(top)
                top_level.append(dest)

            if not parts:
                dest.mkdir(parents=True, exist_ok=True)
                continue

            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("wb") as out:
                for entry in parts:
                    offset, dlen = entry[0], entry[1]
                    prefix = entry[2] if len(entry) >= 3 else b""
                    if prefix:
                        out.write(prefix)
                    f.seek(offset)
                    remaining = dlen
                    while remaining > 0:
                        chunk = f.read(min(remaining, 1 << 20))
                        if not chunk:
                            raise RpaError(f"truncated archive member: {name}")
                        out.write(chunk)
                        remaining -= len(chunk)
    return top_level


# Backwards-friendly alias used by the thin CLI and tests.
extract = extract_archive


def create(rpy_paths: List[Path], out_path: Path, prefix: str = "") -> Path:
    """Pack ``rpy_paths`` into a legacy-trailer RPA archive (test fixtures)."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    index: Dict[str, List[Tuple[int, int]]] = {}

    with out_path.open("wb") as f:
        f.write(RPA_MAGIC)
        f.write(struct.pack("i", 0))
        for p in rpy_paths:
            p = Path(p)
            f.seek(0, os.SEEK_END)
            offset = f.tell()
            with p.open("rb") as src:
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
            length = f.tell() - offset
            arcname = prefix + p.name
            index[arcname] = [(offset, length)]

        f.seek(0, os.SEEK_END)
        pickled = zlib.compress(repr(index).encode("utf-8"))
        f.write(struct.pack("i", len(pickled)))
        f.write(pickled)
    return out_path


if __name__ == "__main__":
    if len(sys.argv) >= 3:
        extract_archive(Path(sys.argv[1]), Path(sys.argv[2]))
        print(f"extracted to {sys.argv[2]}")
    else:
        print("usage: rpatool.py <archive.rpa> <out-dir>")
