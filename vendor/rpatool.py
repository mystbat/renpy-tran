#!/usr/bin/env python3
"""rpatool — Ren'Py archived (.rpa) pack/extract tool.

Faithful, stdlib-only re-implementation of the RPA-3.0 container format used
by Ren'Py games, based on the public-domain reference tool by Shizmob
(https://github.com/Shizmob/rpatool). Vendored as source per ADR-0005 so the
format can be reviewed, licensed and unit-tested without expanding a
base64/cab blob at runtime.

RPA-3.0 file layout
-------------------
    [0:8]    "RPA-3.0 "
    [8:12]   tmpkey  (4-byte int == len(indexlen field + pickled index))
    [12:..]  concatenated file data
    [..:..]  indexlen field (4-byte int == len(pickled index))
    [..:..]  pickled index (zlib-compressed ``repr`` of a dict)

The index maps each archived name to a list of ``(offset, length)`` tuples
with absolute file offsets. ``extract`` and ``create`` are the two entry
points used by ``pytran.unren.Unren.extract_rpa``.
"""

from __future__ import annotations

import os
import struct
import sys
import zlib
from ast import literal_eval
from pathlib import Path
from typing import Dict, List, Tuple

__all__ = ["RpaError", "extract", "create", "extract_archive"]

RPA_MAGIC = b"RPA-3.0 "
HEADER_SIZE = len(RPA_MAGIC) + 4  # magic + tmpkey field


class RpaError(Exception):
    """Raised for malformed or unsupported .rpa archives."""


def _read_index(f) -> Dict[str, List[Tuple[int, int]]]:
    """Read and decode the RPA-3.0 index from an open, positioned file."""
    f.seek(len(RPA_MAGIC))
    tmpkey_raw = f.read(4)
    if len(tmpkey_raw) != 4:
        raise RpaError("not an RPA-3.0 archive (header too short)")
    (tmpkey,) = struct.unpack("i", tmpkey_raw)

    # Trailer layout: [indexlen:4 bytes][pickled index:tmpkey bytes]
    # The trailer is the last (4 + tmpkey) bytes of the file. Seek back to its
    # start, read the 4-byte length prefix, then read exactly tmpkey bytes of
    # pickled index data. (Seeking relative to SEEK_CUR after reading the length
    # would re-read already-consumed bytes and underflow on a short file.)
    f.seek(-(4 + tmpkey), os.SEEK_END)
    (indexlen,) = struct.unpack("i", f.read(4))
    if indexlen != tmpkey:
        raise RpaError(
            f"RPA trailer inconsistent: indexlen={indexlen}, data={tmpkey}"
        )
    data = f.read(tmpkey)
    try:
        decompressed = zlib.decompress(data)
    except zlib.error as exc:  # pragma: no cover - defensive
        raise RpaError(f"failed to decompress RPA index: {exc}") from exc
    try:
        index = literal_eval(decompressed.decode("utf-8"))
    except (ValueError, SyntaxError) as exc:
        raise RpaError(f"failed to parse RPA index: {exc}") from exc
    if not isinstance(index, dict):
        raise RpaError("RPA index is not a mapping")
    return index


def extract_archive(rpa_path: Path, out_dir: Path) -> List[Path]:
    """Extract every member of ``rpa_path`` into ``out_dir``.

    Archived names use ``/`` separators; these are mapped to the OS separator
    and parent directories are created as needed. Returns the list of written
    file paths (top-level entries only, matching ``Unren.extract_rpa``).
    """
    rpa_path = Path(rpa_path)
    out_dir = Path(out_dir)
    if not rpa_path.is_file():
        raise RpaError(f"archive not found: {rpa_path}")

    with rpa_path.open("rb") as f:
        magic = f.read(len(RPA_MAGIC))
        if magic != RPA_MAGIC:
            raise RpaError(f"unsupported archive magic: {magic!r}")
        index = _read_index(f)

        top_level: List[Path] = []
        seen_top: set[str] = set()
        for name, parts in index.items():
            rel = name.replace("/", os.sep)
            dest = out_dir / rel
            # record the top-level entry for the caller
            top = rel.split(os.sep, 1)[0]
            if top not in seen_top:
                seen_top.add(top)
                top_level.append(dest)

            if not parts:
                # directory entry
                dest.mkdir(parents=True, exist_ok=True)
                continue

            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("wb") as out:
                for offset, length in parts:
                    f.seek(offset)
                    remaining = length
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
    """Pack ``rpy_paths`` into a single RPA-3.0 archive at ``out_path``.

    ``prefix`` is prepended to every archived member name (Ren'Py games
    conventionally use an empty prefix and store names such as
    ``game/script.rpyc``). Returns ``out_path``.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    index: Dict[str, List[Tuple[int, int]]] = {}

    with out_path.open("wb") as f:
        f.write(RPA_MAGIC)
        f.write(struct.pack("i", 0))  # tmpkey placeholder, fixed up at end
        data_offset = f.tell()
        del data_offset  # offset is recomputed via seek-to-end per member

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

        # index trailer: indexlen field + zlib-compressed pickled index
        f.seek(0, os.SEEK_END)
        pickled = zlib.compress(repr(index).encode("utf-8"))
        f.write(struct.pack("i", len(pickled)))
        f.write(pickled)

        # fix up the tmpkey field at byte offset 8 (len of trailing payload)
        f.seek(len(RPA_MAGIC))
        f.write(struct.pack("i", len(pickled)))

    return out_path


def main(argv: List[str]) -> int:  # pragma: no cover - thin CLI
    if len(argv) < 3 or argv[1] not in ("-x", "-c"):
        print("usage: rpatool.py -x archive.rpa [out_dir]")
        print("       rpatool.py -c archive.rpa file [file ...]")
        return 2
    mode = argv[1]
    if mode == "-x":
        rpa = Path(argv[2])
        out = Path(argv[3]) if len(argv) > 3 else rpa.parent
        files = extract_archive(rpa, out)
        for fp in files:
            print(fp)
        return 0
    out = Path(argv[2])
    files = [Path(x) for x in argv[3:]]
    create(files, out)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv))
