#!/usr/bin/env python3
"""vendor.unrpyc_real_v1 — upstream unrpyc v1.x (Ren'Py 7/6 fallback), vendored.

    Upstream:  CensoredUsername / unrpyc  (`legacy` line, Unrpyc v1.x)
    Authors:   Yuri K. Schlesner, CensoredUsername, Jackmcbarn
    Tag/Commit: v1.3.2 / 13f7e0ec56a7134a5afc89d70f3e48823e715f3e (2024-08-17)
    License:   MIT (decompiler/codegen.py additionally 3-clause BSD, Armin
               Ronacher) — see ``vendor/unrpyc_real_v1/LICENSE``

Purpose and hard constraint
---------------------------

Unrpyc v1 is the **Ren'Py 7.x / 6.x** decoder. Unrpyc v2 (in
``vendor/unrpyc_real/``) targets Ren'Py 8.x and does not cover those engines, so
both are vendored and :mod:`vendor.unrpyc` falls back to this one when v2 cannot
decode a file.

**This version requires Python 2.7.** Its sources use Python 2 syntax
(``except Exception, e:``) and its ``main()`` explicitly rejects anything but
2.7. It therefore cannot be imported by the pytran interpreter (Python 3) and is
only ever driven from a **subprocess** running a Python 2.7 interpreter —
normally the one the Ren'Py 7 game bundles at
``<game>/lib/windows-i686/python.exe``.

Like v2 it fakes the ``renpy`` package itself
(``decompiler/renpycompat.py`` → ``magic.fake_package("renpy")``), so no real
Ren'Py engine is required — only a Python 2.7 with a working standard library.

Entry point
-----------

* :func:`decompile_file` — decode one ``.rpyc`` to ``dst``. Callable only under
  Python 2.7; raises :class:`UnrpycV1Unavailable` otherwise, so callers get a
  clear reason instead of a ``SyntaxError``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Vendored upstream identification (ADR-0005 / ADR-0008 attribution)
# ---------------------------------------------------------------------------

__version__ = "v1.3.2"
UPSTREAM_COMMIT = "13f7e0ec56a7134a5afc89d70f3e48823e715f3e"
UPSTREAM_TAG = "v1.3.2"
UPSTREAM_DATE = "2024-08-17"
UPSTREAM_LICENSE = "MIT (decompiler/codegen.py: 3-clause BSD)"
# Upstream v1 hard-requires Python 2.7 (py2 syntax + explicit version gate).
PYTHON_REQUIRES = (2, 7)
# Ren'Py versions this tree targets (v2 covers 8.x instead).
TARGETS_RENPY = "Ren'Py 7.x / 6.x"

_HERE = Path(__file__).resolve().parent


class UnrpycV1Unavailable(RuntimeError):
    """Raised when unrpyc v1 cannot be used in the current interpreter."""


def _ensure_on_path() -> None:
    d = str(_HERE)
    if d not in sys.path:
        sys.path.insert(0, d)


def check_available() -> None:
    """Raise :class:`UnrpycV1Unavailable` unless we are on Python 2.7."""
    if sys.version_info[:2] != PYTHON_REQUIRES:
        raise UnrpycV1Unavailable(
            "vendored unrpyc %s requires Python %d.%d; this interpreter is "
            "%d.%d.%d. Run it in a subprocess under the Ren'Py 7 game's own "
            "bundled python (usually <game>/lib/windows-i686/python.exe)."
            % (__version__, PYTHON_REQUIRES[0], PYTHON_REQUIRES[1],
               sys.version_info[0], sys.version_info[1], sys.version_info[2])
        )


def decompile_file(
    src: str,
    dst: str,
    try_harder: bool = False,
    init_offset: bool = True,
) -> Optional[str]:
    """Decode one ``.rpyc`` into ``dst``. Requires Python 2.7.

    Returns the produced ``.rpy`` path, or ``None`` if nothing was produced.
    """
    import os

    check_available()
    _ensure_on_path()
    import unrpyc  # type: ignore  # upstream v1 module (vendored verbatim)

    src_path = Path(src)
    dst_path = Path(dst)
    dst_path.parent.mkdir(parents=True, exist_ok=True)

    # Stage beside dst in an existing writable directory when possible (avoids
    # mkdir, which sandboxes may deny); fall back to a temp dir otherwise.
    stage_name = "%s.unrpyc_stage%s" % (src_path.stem, src_path.suffix)
    target = dst_path.parent / stage_name
    try:
        probe = dst_path.parent / ".unrpyc_write_probe"
        probe.write_bytes(b"")
        probe.unlink()
        writable = True
    except OSError:
        writable = False

    td = None
    if not writable:
        import tempfile

        base = os.environ.get("UNRPYC_WORK_DIR") or None
        td = tempfile.mkdtemp(prefix="unrpyc_v1_work_", dir=base)
        target = Path(td) / stage_name

    try:
        target.write_bytes(src_path.read_bytes())
        context = unrpyc.Context()
        unrpyc.decompile_rpyc(
            str(target),
            context,
            overwrite=True,
            init_offset=init_offset,
            try_harder=try_harder,
        )
        if context.error is not None:
            raise context.error
        produced = target.with_suffix(".rpy")
        if not produced.exists():
            return None
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        dst_path.write_bytes(produced.read_bytes())
        return str(dst_path)
    finally:
        for p in (target, target.with_suffix(".rpy")):
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass
        if td is not None:
            import shutil

            try:
                shutil.rmtree(td, ignore_errors=True)
            except OSError:
                pass
