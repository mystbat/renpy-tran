#!/usr/bin/env python3
"""vendor.unrpyc_real — the REAL upstream unrpyc, vendored as auditable source.

This package is the upstream unrpyc decompiler, vendored **unmodified** from a
release tag (no local compatibility patches are needed any more):

    Upstream:  CensoredUsername / unrpyc
    Authors:   Yuri K. Schlesner, CensoredUsername, Jackmcbarn (per upstream
               file headers)
    Branch:    master  (Unrpyc v2.x — Python 3 / Ren'Py 8 toolchain)
    Tag/Commit: v2.0.4 / 3ae8334ed71a05535927dcc559663d3aca51215b
    Date:      2026-02-23
    License:   MIT  — see ``vendor/unrpyc_real/LICENSE``

It decodes Ren'Py ``.rpyc`` / ``.rpymc`` bytecode back into editable ``.rpy``
source.

Why v2 is a significant upgrade over the previously vendored 2019 tree
(commit ``44febb0``):

* **No Ren'Py engine required.** v1 imported ``renpy.object`` at module load and
  so could only run inside an interpreter holding a real Ren'Py engine, forcing
  the §8.5 subprocess-bridge. v2 installs a *fake* ``renpy`` package itself
  (``decompiler/renpycompat.py`` → ``magic.fake_package("renpy")``) and rebuild
  the AST from the pickle stream with its own compatibility classes, so it runs
  directly inside the plain pytran interpreter. It only needs Python ≥ 3.9.
* **Actually supports Ren'Py 8.x ASTs.** v2 models the modern node layout
  (``Label._name``, ``Signature.extrapos``, ``Style.take``,
  ``PyExpr.linenumber``, ``renpy.astsupport`` …), which the 2019 tree could not
  and which made it fail with ``AttributeError`` on many 8.x files.
* **``--try-harder`` works on Python 3.** v1's obfuscation path used Python-2
  idioms (``e.message``, ``ord()`` on ints, int-vs-str byte comparison) that
  crashed on every file; v2's ``deobfuscate.py`` is Python 3 throughout.
* **Correct RPYC2 slot handling.** v1 fed the whole slot *dict* to ``zlib``
  (``codecs.decode(chunks, 'zlib')``), which surfaced as
  ``TypeError: a bytes-like object is required, not 'dict'`` or — depending on
  how much of the stream had been consumed — as
  ``Error -5 while decompressing data: incomplete or truncated stream``.
  v2 selects ``chunks[1]`` explicitly and raises a typed ``BadRpycException``
  when the header is genuinely unusable.

Entry points:

* :func:`decompile_file` — in-process decode of one ``.rpyc`` into ``dst``.
  Runs in the **current** interpreter (no subprocess needed).
* :mod:`vendor.unrpyc_real.runner` — CLI wrapper retained for callers that still
  want process isolation (e.g. to run under the game's own bundled Python).

See ``vendor/README.md`` and ``docs/adr/ADR-0008-*.md`` for the rationale.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Vendored upstream identification (ADR-0005 / ADR-0008 attribution)
# ---------------------------------------------------------------------------

# Upstream tag this vendored copy was taken from.
__version__ = "v2.0.4"
UPSTREAM_COMMIT = "3ae8334ed71a05535927dcc559663d3aca51215b"
UPSTREAM_TAG = "v2.0.4"
UPSTREAM_DATE = "2026-02-23"
UPSTREAM_LICENSE = "MIT"
# Minimum python required by upstream v2 (see upstream unrpyc.py main()).
PYTHON_REQUIRES = (3, 9)

# Abs directory of this package; inserted on sys.path so the upstream
# ``unrpyc.py`` / ``decompiler`` subpackage can be imported by relative name.
_HERE = Path(__file__).resolve().parent


def _ensure_on_path() -> None:
    """Make sure this package directory is importable as a top-level path."""
    d = str(_HERE)
    if d not in sys.path:
        sys.path.insert(0, d)


def _require_supported_python() -> None:
    """Fail early and clearly if the interpreter is too old for upstream v2."""
    if sys.version_info < PYTHON_REQUIRES:
        raise RuntimeError(
            "vendored unrpyc %s requires Python %d.%d or newer; "
            "this interpreter is %d.%d.%d"
            % (__version__, PYTHON_REQUIRES[0], PYTHON_REQUIRES[1],
               sys.version_info[0], sys.version_info[1], sys.version_info[2])
        )


def _is_writable_dir(d: Path) -> bool:
    """Cheap probe: can we create/overwrite a file in this existing directory?"""
    try:
        probe = d / ".unrpyc_write_probe"
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


def _stage_for_decode(src_path: Path, dst_path: Path):
    """Return ``(staged_source_path, temp_dir_or_None)`` for the decode.

    The upstream writes ``<stem>.rpy`` beside its input. Preferred stage
    location is ``dst.parent`` (an existing, writable directory), which avoids
    creating any new directory — important under sandboxes that permit file
    writes but deny ``mkdir``. Falls back to a temp scratch dir when
    ``dst.parent`` is not writable (e.g. a read-only shipped game).

    The staging name never collides with ``dst``: it carries an
    ``.unrpyc_stage`` marker inside the stem (upstream turns ``<stem>.rpyc``
    into ``<stem>.rpy``, so the ``.rpyc`` suffix is kept for extension
    selection). Staging beside the destination is therefore safe even though
    upstream writes its output next to its input.
    """
    # Staging name must NOT collide with dst or with upstream's sibling output:
    # upstream turns <stem>.rpyc into <stem>.rpy, so we keep the .rpyc suffix
    # (needed for extension selection) but mark the stem.
    stage_name = "%s.unrpyc_stage%s" % (src_path.stem, src_path.suffix)
    target = dst_path.parent / stage_name
    if _is_writable_dir(dst_path.parent):
        target.write_bytes(src_path.read_bytes())
        return target, None

    # Fallback: scratch dir under the OS temp root (or UNRPYC_WORK_DIR).
    import os

    base = os.environ.get("UNRPYC_WORK_DIR") or None
    td = Path(tempfile.mkdtemp(prefix="unrpyc_work_", dir=base))
    work = td / (src_path.stem + src_path.suffix)
    work.write_bytes(src_path.read_bytes())
    return work, td


def _cleanup_staging(staged, produced_suffix=".rpy", td=None) -> None:
    """Remove the staged copy, its produced artifact, and the temp dir."""
    try:
        Path(staged).unlink(missing_ok=True)  # type: ignore[call-arg]
    except OSError:
        pass
    try:
        Path(staged).with_suffix(produced_suffix).unlink(missing_ok=True)  # type: ignore[call-arg]
    except OSError:
        pass
    if td is not None:
        import shutil

        try:
            shutil.rmtree(td, ignore_errors=True)
        except OSError:
            pass


def decompile_file(
    src: str,
    dst: str,
    try_harder: bool = False,
    init_offset: bool = True,
) -> Optional[str]:
    """In-process decompile of one ``.rpyc`` to ``dst``.

    Unlike the previously vendored 2019 upstream, this does **not** need a real
    Ren'Py engine: upstream v2 reconstructs the AST itself and therefore runs
    inside the ordinary pytran interpreter (Python ≥ 3.9).

    The upstream writes its ``.rpy`` next to its input, so the source is staged
    beside the destination (``dst.parent``) when that directory is writable, and
    only falls back to a scratch directory under the OS temp root otherwise.
    Staging next to ``dst`` avoids creating directories at all, which matters in
    sandboxed environments where new directories are denied but file writes in
    existing paths are allowed. Set ``UNRPYC_WORK_DIR`` to relocate the temp
    fallback root.

    Returns the produced ``.rpy`` path, or ``None`` on failure.
    """
    _require_supported_python()
    _ensure_on_path()
    import unrpyc  # type: ignore  # upstream module (vendored verbatim)

    src_path = Path(src)
    dst_path = Path(dst)
    dst_path.parent.mkdir(parents=True, exist_ok=True)

    staged, td = _stage_for_decode(src_path, dst_path)
    try:
        # Upstream v2 API: decompile_rpyc(filename: Path, context, ...).
        # Exceptions propagate to the caller (and are mirrored onto
        # ``context.error``), so failures are never silently swallowed.
        context = unrpyc.Context()
        unrpyc.decompile_rpyc(
            Path(staged),
            context,
            overwrite=True,
            init_offset=init_offset,
            try_harder=try_harder,
        )
        # Surface upstream's own recorded error even if it did not raise.
        if context.error is not None:
            raise context.error

        produced_path = Path(staged).with_suffix(".rpy")
        if not produced_path.exists():
            return None
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        dst_path.write_bytes(produced_path.read_bytes())
        return str(dst_path)
    finally:
        _cleanup_staging(staged, produced_suffix=".rpy", td=td)


def main(argv: Optional[list] = None) -> int:
    """Console entry: ``python -m vendor.unrpyc_real src [src ...]``."""
    if argv is None:
        argv = sys.argv[1:]
    _require_supported_python()
    _ensure_on_path()
    import unrpyc  # type: ignore  # upstream module (vendored verbatim)

    if not argv:
        print("usage: python -m vendor.unrpyc_real file.rpyc [file.rpyc ...]")
        return 2
    ok = 0
    for a in argv:
        p = Path(a)
        context = unrpyc.Context()
        try:
            unrpyc.decompile_rpyc(p, context, overwrite=False, init_offset=True)
            if context.error is not None:
                raise context.error
            ok += 1
        except Exception as exc:  # noqa: BLE001 - CLI reports failures per file
            print("error decompiling %s: %s: %s" % (a, type(exc).__name__, exc))
    return 0 if ok == len(argv) else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
