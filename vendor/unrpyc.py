#!/usr/bin/env python3
"""unrpyc — Ren'Py .rpyc decompiler entry point (vendored, thin wrapper).

This module is the project-facing thin wrapper described by ADR-0008. It keeps
the W4 contract testable while preserving a real, auditable drop-in upgrade
path to the genuine upstream unrpyc (CensoredUsername + F95Sam, MIT), which is
vendored — source only — into ``vendor/unrpyc_real/`` (ADR-0005).

Two decode paths:

1. **Synthetic fixture fast-path (default).** The ``.rpyc`` fixtures produced by
   ``tests/fixtures/_build_fake_game.py`` embed their original ``.rpy`` source
   inside a marker block (``# src_begin`` … ``# src_end``). We recover that
   source verbatim. This keeps parser + write-back round-trip tests green
   without invoking the heavy real decompiler.
2. **Real upstream (subprocess bridge).** When the genuine upstream is present
   in ``vendor/unrpyc_real/``, real ``.rpyc`` (e.g. from a shipped game) is
   decoded by spawning that package's ``runner.py`` in a *separate process*
   whose ``sys.path`` includes a real Ren'Py engine. This is required because
   the upstream does ``import renpy.object`` at import time and therefore
   cannot be imported into the project interpreter (which has no Ren'Py). The
   pattern mirrors the design §8.5 SDK-bridge: the real engine lives in the
   game / Ren'Py SDK, not in pytran.

Callers pass ``python_exe`` and ``sdk_root`` for the real path (the project
locates them via ``pytran.unren.UnrenImpl``: the game's bundled interpreter and
the directory containing ``renpy/``). When the real upstream is present but no
``python_exe``/``sdk_root`` is supplied (and the file is not a synthetic
fixture), a clear :class:`UnrpycError` explains how to enable real decode.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

__all__ = ["UnrpycError", "decompile_file", "decompile_files", "main"]

# Marker block wrapping the original .rpy source inside a synthetic fixture.
SRC_BEGIN = "# src_begin"
SRC_END = "# src_end"

# Directory (not module name) where the REAL upstream unrpyc lives. The upstream
# imports renpy at load time, so it is referenced by directory and invoked in a
# subprocess — never imported into this interpreter.
REAL_DIR = Path(__file__).resolve().parent / "unrpyc_real"

# repo root = vendor/..  (needed to `python -m vendor.unrpyc_real.runner`)
_REPO_ROOT = Path(__file__).resolve().parents[1]


class UnrpycError(Exception):
    """Raised when a .rpyc cannot be decompiled by the vendored tooling."""


def _real_unrpyc_dir() -> Optional[Path]:
    """Return the real upstream package dir if it has been vendored in.

    We check for the *directory* (plus its LICENSE/runner), not an import,
    because the upstream imports ``renpy`` at module load and so is unusable
    from the project interpreter. See ADR-0008.
    """
    if REAL_DIR.is_dir() and (REAL_DIR / "unrpyc.py").is_file():
        return REAL_DIR
    return None


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


def _run_real_subprocess(
    jobs: List[dict],
    python_exe: Path,
    sdk_root: Path,
    try_harder: bool,
) -> List[dict]:
    """Decode real ``.rpyc`` jobs via ``vendor.unrpyc_real.runner`` subprocess.

    ``jobs`` is a list of ``{"src", "dst"}`` dicts. Returns the runner's parsed
    JSON list of ``{"src", "dst", "ok", "error"}`` records.
    """
    cmd = [
        str(python_exe),
        "-m", "vendor.unrpyc_real.runner",
        "--sdk-root", str(sdk_root),
    ]
    if try_harder:
        cmd.append("--try-harder")
    cmd.append("--jobs")
    cmd.append("-")  # read jobs JSON from stdin
    proc = subprocess.run(
        cmd,
        input=json.dumps(jobs, ensure_ascii=False),
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
    )
    if proc.returncode not in (0, 1):
        raise UnrpycError(
            f"real unrpyc runner failed (exit {proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    # The runner prints exactly one JSON line; ignore any stray warning lines.
    for line in reversed(proc.stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("["):
            return json.loads(line)
    if proc.stderr.strip():
        raise UnrpycError(f"real unrpyc runner error: {proc.stderr.strip()}")
    return []


def decompile_file(
    rpyc_path: Path,
    out_dir: Optional[Path] = None,
    try_harder: bool = False,
    python_exe: Optional[Path] = None,
    sdk_root: Optional[Path] = None,
) -> Optional[Path]:
    """Decompile a single ``.rpyc`` to a ``.rpy`` next to it.

    Resolution order:

    * If ``rpyc_path`` is a synthetic fixture (embedded ``# src_begin`` block),
      recover its source verbatim — no upstream needed.
    * Else if the real upstream is vendored, spawn its ``runner`` subprocess
      (needs ``python_exe`` + ``sdk_root``) to decode the real bytecode.
    * Else raise :class:`UnrpycError` explaining the missing drop-in.

    Returns the produced ``.rpy`` path, or ``None`` if nothing was produced.
    """
    rpyc_path = Path(rpyc_path)
    if out_dir is None:
        out_dir = rpyc_path.parent
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / (rpyc_path.stem + ".rpy")

    # 1) synthetic fixture fast-path — keeps fixtures green without real upstream
    embedded = _extract_embedded_src(rpyc_path)
    if embedded is not None:
        out_path.write_text(embedded + "\n", encoding="utf-8")
        return out_path

    # 2) real upstream (subprocess bridge — mirrors design §8.5)
    real_dir = _real_unrpyc_dir()
    if real_dir is not None:
        if python_exe is None or sdk_root is None:
            raise UnrpycError(
                f"cannot decompile real game .rpyc {rpyc_path.name!r}: the real "
                f"upstream unrpyc is vendored at vendor/unrpyc_real/ but it needs "
                f"a Ren'Py interpreter and engine. Pass python_exe= (game's "
                f"bundled python or a Ren'Py SDK python) and sdk_root= (the dir "
                f"containing renpy/) — see ADR-0008 / design §8.5."
            )
        records = _run_real_subprocess(
            [{"src": str(rpyc_path), "dst": str(out_path)}],
            Path(python_exe),
            Path(sdk_root),
            try_harder=try_harder,
        )
        rec = records[0] if records else {}
        if not rec.get("ok"):
            raise UnrpycError(
                f"real unrpyc failed to decompile {rpyc_path.name!r}: "
                f"{rec.get('error') or 'unknown error'}"
            )
        return out_path

    raise UnrpycError(
        f"cannot decompile {rpyc_path.name!r}: vendored unrpyc handles only "
        f"synthetic fixtures; drop the real upstream unrpyc into vendor/"
        f"unrpyc_real/ (ADR-0008)"
    )


def decompile_files(
    rpyc_paths: List[Path],
    out_dir: Optional[Path] = None,
    try_harder: bool = False,
    python_exe: Optional[Path] = None,
    sdk_root: Optional[Path] = None,
) -> List[Path]:
    """Decompile many ``.rpyc`` files. Raises on the first unrecoverable one."""
    produced: List[Path] = []

    # Split into synthetic (verbatim) and real (subprocess) jobs so a single
    # subprocess call covers every real file.
    real_jobs: List[dict] = []
    for p in rpyc_paths:
        p = Path(p)
        if out_dir is None:
            dst = p.parent / (p.stem + ".rpy")
        else:
            dst = Path(out_dir) / (p.stem + ".rpy")
        if _extract_embedded_src(p) is not None:
            produced.append(decompile_file(p, out_dir=out_dir))
        else:
            real_jobs.append({"src": str(p), "dst": str(dst)})

    real_dir = _real_unrpyc_dir()
    if real_jobs:
        if real_dir is None:
            raise UnrpycError(
                "cannot decompile real game .rpyc: drop the real upstream "
                "unrpyc into vendor/unrpyc_real/ (ADR-0008)"
            )
        if python_exe is None or sdk_root is None:
            raise UnrpycError(
                "cannot decompile real game .rpyc: pass python_exe= (a Ren'Py "
                "interpreter) and sdk_root= (dir containing renpy/) — ADR-0008."
            )
        records = _run_real_subprocess(
            real_jobs, Path(python_exe), Path(sdk_root), try_harder=try_harder
        )
        by_src = {r.get("src"): r for r in records}
        for job in real_jobs:
            rec = by_src.get(job["src"], {})
            if not rec.get("ok"):
                raise UnrpycError(
                    f"real unrpyc failed to decompile "
                    f"{Path(job['src']).name!r}: {rec.get('error') or 'unknown'}"
                )
            produced.append(Path(job["dst"]))
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
