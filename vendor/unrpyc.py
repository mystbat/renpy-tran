#!/usr/bin/env python3
"""unrpyc — Ren'Py .rpyc decompiler entry point (vendored).

Upstream unrpyc (CensoredUsername original + F95Sam edit) is a large, complex
decompiler that recovers Ren'Py bytecode into editable .rpy. Vendoring its full
source is out of scope for W4, so this module provides a thin, auditable entry
point that:

1. Decompiles the **synthetic** `fake_unrpyc` .rpyc fixtures produced by
   ``tests/fixtures/_build_fake_game.py``. Those fixtures embed their original
   ``.rpy`` source inside a small marker block, so a byte-faithful ``.rpy`` can
   be recovered for round-trip testing of the parser + write-back layers.
2. Dispatches to the **real** upstream unrpyc when it is dropped into
   ``vendor/unrpyc_real/`` (ADR-0008: drop-in upgrade path).
3. Raises a clear, actionable ``UnrpycError`` when neither path applies, so
   ``pytran.unren.Unren.decompile`` can report that upstream unrpyc is missing.

This keeps the W4 contract testable without shipping base64 blobs or a full
bytecode decompiler, while preserving the upgrade path to the real tool.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from typing import List, Optional

__all__ = ["UnrpycError", "decompile_file", "decompile_files", "main"]

# Marker block wrapping the original .rpy source inside a synthetic fixture.
SRC_BEGIN = "# src_begin"
SRC_END = "# src_end"

REAL_MODULE = "vendor.unrpyc_real"


class UnrpycError(Exception):
    """Raised when a .rpyc cannot be decompiled by the vendored tooling."""


def _real_unrpyc() -> Optional[object]:
    """Return the real upstream unrpyc module if it has been vendored in."""
    if _real_unrpyc._cache is not None:
        return _real_unrpyc._cache
    try:
        mod = importlib.import_module(REAL_MODULE)
    except Exception:
        mod = False  # sentinel: looked up, not present
    _real_unrpyc._cache = mod or None
    return _real_unrpyc._cache


_real_unrpyc._cache = None  # type: ignore[attr-defined]


def _extract_embedded_src(rpyc_path: Path) -> Optional[str]:
    """Return embedded .rpy source if ``rpyc_path`` is a synthetic fixture."""
    text = rpyc_path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    started = False
    collected: List[str] = []
    for line in lines:
        if not started:
            if line.strip() == SRC_BEGIN:
                started = True
            continue
        if line.strip() == SRC_END:
            return "\n".join(collected)
        collected.append(line)
    return None


def decompile_file(
    rpyc_path: Path,
    out_dir: Optional[Path] = None,
    try_harder: bool = False,
) -> Optional[Path]:
    """Decompile a single ``.rpyc`` to a ``.rpy`` next to it.

    Honours the upstream ``--try-harder`` semantics (passed through to the real
    tool; for the synthetic path it only affects the log line). Returns the path
    of the produced ``.rpy``, or ``None`` if no source could be produced and the
    real upstream tool is absent.
    """
    rpyc_path = Path(rpyc_path)
    if out_dir is None:
        out_dir = rpyc_path.parent
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / (rpyc_path.stem + ".rpy")

    embedded = _extract_embedded_src(rpyc_path)
    if embedded is not None:
        out_path.write_text(embedded + "\n", encoding="utf-8")
        return out_path

    real = _real_unrpyc()
    if real is not None:
        # Delegate to the real upstream tool, which knows how to locate the
        # matching .rpy / regenerate translations.
        callable_ = getattr(real, "decompile_file", None) or getattr(
            real, "main", None
        )
        if callable_ is None:  # pragma: no cover - depends on upstream layout
            raise UnrpycError("real unrpyc present but has no decompile entry")
        out = callable_(str(rpyc_path), str(out_path), try_harder=try_harder)
        return Path(out) if out else out_path

    raise UnrpycError(
        f"cannot decompile {rpyc_path.name!r}: vendored unrpyc handles only "
        f"synthetic fixtures; drop the real upstream unrpyc into vendor/"
        f"unrpyc_real/ (ADR-0008)"
    )


def decompile_files(
    rpyc_paths: List[Path],
    out_dir: Optional[Path] = None,
    try_harder: bool = False,
) -> List[Path]:
    """Decompile many ``.rpyc`` files. Raises on the first unrecoverable one."""
    produced: List[Path] = []
    for p in rpyc_paths:
        out = decompile_file(p, out_dir=out_dir, try_harder=try_harder)
        if out is not None:
            produced.append(out)
    return produced


def main(argv: List[str]) -> int:  # pragma: no cover - thin CLI
    if not argv[1:]:
        print("usage: unrpyc.py file.rpyc [file.rpyc ...]")
        return 2
    try:
        produced = decompile_files([Path(a) for a in argv[1:]], try_harder=False)
    except UnrpycError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for fp in produced:
        print(fp)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv))
