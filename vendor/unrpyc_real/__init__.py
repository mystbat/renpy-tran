#!/usr/bin/env python3
"""vendor.unrpyc_real — the REAL upstream unrpyc, vendored as auditable source.

This package is the unmodified (save for three small compatibility shims,
marked ``W4b compatibility``) upstream unrpyc decompiler:

    Upstream:  CensoredUsername / unrpyc  (original, Yuri K. Schlesner, MIT)
    Edit:      F95Sam / UnRen-forall fork
    Commit:    44febb0  (2019-10-07)
    License:   MIT  — see ``vendor/unrpyc_real/LICENSE``

It decodes Ren'Py ``.rpyc`` / ``.rpymc`` bytecode back into editable ``.rpy``
source. Because the upstream module does ``import renpy.object`` at import
time, it **cannot** be imported into the pytran project interpreter (which has
no Ren'Py engine). It must therefore run in a *subprocess* whose ``sys.path``
includes a real Ren'Py installation (the design §8.5 SDK-bridge pattern).

Two entry points are provided:

* :func:`decompile_file` — a thin, in-process shim used when the caller *is*
  already running inside a Python that can import ``renpy`` (e.g. the Ren'Py
  SDK's bundled interpreter). It inserts this package on ``sys.path`` and calls
  the upstream ``decompile_rpyc`` API.
* :mod:`vendor.unrpyc_real.runner` — a CLI wrapper spawned by
  ``vendor.unrpyc`` so the project interpreter can drive the real decompiler
  through a subprocess without importing ``renpy`` itself.

See ``vendor/README.md`` and ``docs/adr/ADR-0008-*.md`` for the rationale.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Vendored upstream identification (ADR-0005 / ADR-0008 attribution)
# ---------------------------------------------------------------------------

__version__ = "44febb0"  # upstream commit this vendored copy was taken from
UPSTREAM_COMMIT = "44febb0"
UPSTREAM_DATE = "2019-10-07"
UPSTREAM_LICENSE = "MIT"

# Abs directory of this package; inserted on sys.path so the upstream
# ``unrpyc.py`` / ``decompiler`` subpackage can be imported by relative name.
_HERE = Path(__file__).resolve().parent


def _ensure_on_path() -> None:
    """Make sure this package directory is importable as a top-level path."""
    d = str(_HERE)
    if d not in sys.path:
        sys.path.insert(0, d)


def decompile_file(
    src: str,
    dst: str,
    try_harder: bool = False,
    init_offset: bool = True,
) -> Optional[str]:
    """In-process decompile of one ``.rpyc`` to ``dst``.

    Only valid when the *current* interpreter can ``import renpy`` (e.g. the
    Ren'Py SDK's bundled python). The upstream ``unrpyc`` writes its ``.rpy``
    next to the source; shipped/SDK game dirs are often read-only, so we copy
    the source ``.rpyc`` into a writable temp dir, decode there, and copy the
    produced ``.rpy`` out to ``dst``.

    Returns the produced ``.rpy`` path, or ``None`` on failure.
    """
    import shutil

    _ensure_on_path()
    import unrpyc  # type: ignore

    src = Path(src)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)

    # Work in a per-job scratch dir under the OS temp root (NOT the game
    # directory): the game dir may be read-only, may sit under a sandbox that
    # denies new dirs, and parallel/nested job staging produced
    # ``.unrpyc_real_work/.unrpyc_real_work`` conflicts (user-reported crash).
    # The upstream writes its .rpy next to the scratch source; we then copy the
    # result out to ``dst`` and remove the scratch dir.
    import tempfile

    # Scratch dir under the OS temp root (writable for the user running the
    # game; never the read-only game dir). UNRPYC_WORK_DIR overrides for
    # sandboxed/locked-down environments where the default temp root is denied.
    td = Path(tempfile.mkdtemp(prefix="unrpyc_work_"))
    work = td / "src.rpyc"
    work.write_bytes(src.read_bytes())
    produced = unrpyc.decompile_rpyc(
        str(work),
        overwrite=True,
        init_offset=init_offset,
        try_harder=try_harder,
    )
    result: Optional[str] = None
    if produced:
        produced_path = work.with_suffix(".rpy")
        if produced_path.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(produced_path.read_bytes())
            result = str(dst)
    # best-effort cleanup of the scratch dir
    try:
        shutil.rmtree(td, ignore_errors=True)
    except OSError:
        pass
    return result




def main(argv: Optional[list] = None) -> int:
    """Console entry: ``python -m vendor.unrpyc_real src [src ...]``.

    Each ``src`` is decoded next to itself (``<stem>.rpy``) using the upstream
    CLI defaults. This is only usable from a python that can import ``renpy``.
    """
    if argv is None:
        argv = sys.argv[1:]
    _ensure_on_path()
    import unrpyc  # type: ignore

    if not argv:
        print("usage: python -m vendor.unrpyc_real file.rpyc [file.rpyc ...]")
        return 2
    ok = 0
    for a in argv:
        if unrpyc.decompile_rpyc(str(a), overwrite=False, init_offset=True):
            ok += 1
    return 0 if ok == len(argv) else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
