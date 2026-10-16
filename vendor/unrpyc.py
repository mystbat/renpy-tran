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
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

__all__ = ["UnrpycError", "decompile_file", "decompile_files", "main"]

# Marker block wrapping the original .rpy source inside a synthetic fixture.
SRC_BEGIN = "# src_begin"
SRC_END = "# src_end"

# Directory (not module name) where the REAL upstream unrpyc v2 lives
# (Ren'Py 8.x; Python >= 3.9; runs in-process).
REAL_DIR = Path(__file__).resolve().parent / "unrpyc_real"

# Directory where upstream unrpyc **v1** lives — the Ren'Py 7.x / 6.x fallback.
# v1 is Python-2.7 source, so it can never be imported here; it is only driven
# from a subprocess under a Python 2.7 interpreter.
REAL_DIR_V1 = Path(__file__).resolve().parent / "unrpyc_real_v1"

# repo root = vendor/..  (needed to `python -m vendor.unrpyc_real.runner`)
_REPO_ROOT = Path(__file__).resolve().parents[1]


class UnrpycError(Exception):
    """Raised when a .rpyc cannot be decompiled by the vendored tooling."""


def _real_unrpyc_dir() -> Optional[Path]:
    """Return the v2 upstream dir (Ren'Py 8.x) if it has been vendored in."""
    if REAL_DIR.is_dir() and (REAL_DIR / "unrpyc.py").is_file():
        return REAL_DIR
    return None


def _v1_unrpyc_dir() -> Optional[Path]:
    """Return the v1 upstream dir (Ren'Py 7/6 fallback) if vendored in.

    v1 is Python 2.7 source, so it is referenced by directory only — never
    imported into this interpreter.
    """
    if REAL_DIR_V1.is_dir() and (REAL_DIR_V1 / "unrpyc.py").is_file():
        return REAL_DIR_V1
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


def _probe_python27(exe: Path) -> bool:
    """Return True if ``exe`` is a *usable* Python 2.7 interpreter.

    "Usable" matters: Ren'Py 7 games bundle a ``python.exe`` whose standard
    library is stripped (often no ``encodings``), so ``import site`` fails and
    the interpreter cannot run anything. We therefore actually execute a tiny
    probe rather than trusting the filename.
    """
    try:
        proc = subprocess.run(
            [str(exe), "-c", "import sys, os, zlib, pickle;"
                            " sys.stdout.write('ok')"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and "ok" in proc.stdout


def _find_python27(game_root: Optional[Path] = None) -> List[Path]:
    """Candidate Python 2.7 interpreters for the v1 fallback, best first.

    Looks at (a) an explicit override, (b) the game's own bundled interpreter
    — Ren'Py 7 ships ``lib/windows-i686/python.exe`` — and (c) anything named
    ``python2``/``python2.7`` on PATH. Each candidate is probed, because many
    bundled interpreters are unusable (stripped stdlib).
    """
    candidates: List[Path] = []
    seen = set()

    def add(p: Optional[Path]) -> None:
        if p is None:
            return
        try:
            rp = Path(p).resolve()
        except OSError:
            return
        if rp in seen:
            return
        seen.add(rp)
        candidates.append(rp)

    override = os.environ.get("PYTRAN_PYTHON27")
    if override:
        add(Path(override))

    if game_root is not None:
        root = Path(game_root)
        for pattern in (
            "lib/windows-i686/python.exe",
            "lib/py2-windows-x86_64/python.exe",
            "lib/py2-windows-i686/python.exe",
            "lib/windows-x86_64/python.exe",
        ):
            add(root / pattern)
        lib = root / "lib"
        if lib.is_dir():
            for sub in sorted(lib.iterdir()):
                if sub.is_dir() and sub.name.startswith(("py2", "windows")):
                    add(sub / "python.exe")
    for name in ("python2", "python2.7"):
        found = shutil.which(name)
        if found:
            add(Path(found))

    return [p for p in candidates if _probe_python27(p)]


def _run_v1_subprocess(
    jobs: List[dict], python27: Path, try_harder: bool
) -> List[dict]:
    """Decode jobs with upstream v1 in a Python 2.7 subprocess.

    Returns a list of ``{"src", "dst", "ok", "error"}`` records.
    """
    v1_dir = _v1_unrpyc_dir()
    if v1_dir is None:  # pragma: no cover - guarded by caller
        return [{"src": j["src"], "dst": j["dst"], "ok": False,
                 "error": "v1 upstream not vendored"} for j in jobs]

    cmd = [str(python27), str(v1_dir / "runner_v1.py")]
    if try_harder:
        cmd.append("--try-harder")
    cmd += ["--jobs", "-"]

    # The py2.7 worker must not inherit this interpreter's (Python 3.13) virtualenv
    # environment, which otherwise pollutes DLL search / startup and segfaults the
    # old interpreter. Pass a minimal, isolated environment keyed off the worker's
    # own directory.
    env = {
        "PATH": os.path.dirname(str(python27)),
        "SystemRoot": os.environ.get("SystemRoot", r"C:\Windows"),
        "TEMP": os.environ.get("TEMP", str(_REPO_ROOT)),
        "TMP": os.environ.get("TMP", str(_REPO_ROOT)),
    }
    if "PYTRAN_PYTHON27" in os.environ:
        env["PYTRAN_PYTHON27"] = os.environ["PYTRAN_PYTHON27"]
    if "UNRPYC_WORK_DIR" in os.environ:
        env["UNRPYC_WORK_DIR"] = os.environ["UNRPYC_WORK_DIR"]
    try:
        proc = subprocess.run(
            cmd,
            input=json.dumps(jobs, ensure_ascii=False),
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
            env=env,
            timeout=1800,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [{"src": j["src"], "dst": j["dst"], "ok": False,
                 "error": "v1 worker could not start: %s" % exc} for j in jobs]

    for line in reversed(proc.stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("["):
            try:
                return json.loads(line)
            except ValueError:
                break
    # No JSON payload: report the stderr succinctly for every job.
    why = (proc.stderr or proc.stdout or "").strip()[-500:] or \
        "v1 worker exited %s" % proc.returncode
    return [{"src": j["src"], "dst": j["dst"], "ok": False, "error": why}
            for j in jobs]


def _decompile_v1_fallback(
    jobs: List[dict], try_harder: bool, game_root: Optional[Path] = None
) -> List[Path]:
    """Last-resort decode with upstream v1 (Ren'Py 7/6), in a py2.7 subprocess.

    Returns the .rpy paths that v1 managed to produce; failures are reported by
    the caller.
    """
    if _v1_unrpyc_dir() is None:
        return []
    interpreters = _find_python27(game_root)
    if not interpreters:
        return []
    records: List[dict] = []
    for py27 in interpreters:
        records = _run_v1_subprocess(jobs, py27, try_harder)
        if records and all(r.get("ok") for r in records):
            break
    return [Path(r["dst"]) for r in records if r.get("ok")]


def _decompile_in_process(
    rpyc_path: Path, out_path: Path, try_harder: bool
) -> Path:
    """Decode one real ``.rpyc`` with the vendored upstream, in this process.

    Upstream v2.0.4 no longer imports a real ``renpy`` engine (it installs a
    fake ``renpy`` package itself), so the decode can happen here directly.
    Only requires Python >= 3.9. Raises :class:`UnrpycError` on failure.
    """
    try:
        from vendor.unrpyc_real import decompile_file  # type: ignore
    except Exception as exc:  # noqa: BLE001 - surface import failures clearly
        raise UnrpycError(
            f"cannot load vendored unrpyc for {rpyc_path.name!r}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    try:
        result = decompile_file(
            str(rpyc_path), str(out_path), try_harder=try_harder
        )
    except Exception as exc:  # noqa: BLE001 - surface any decode failure
        raise UnrpycError(
            f"real unrpyc failed to decompile {rpyc_path.name!r}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if result is None or not Path(out_path).exists():
        raise UnrpycError(
            f"real unrpyc produced no output for {rpyc_path.name!r}"
        )
    return Path(out_path)


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
    * Else if the real upstream is vendored, decode it. Upstream v2 (since the
      v2.0.4 upgrade) reconstructs the AST itself and needs **no** Ren'Py
      engine, so it runs in-process. A subprocess is used only when the caller
      explicitly supplies ``python_exe`` (e.g. to decode under the game's own
      bundled interpreter).
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

    # 2) real upstream — in-process by default (v2 needs no Ren'Py engine).
    #    A subprocess is used only when the caller pins python_exe explicitly.
    real_dir = _real_unrpyc_dir()
    if real_dir is not None:
        if python_exe is not None:
            if sdk_root is None:
                raise UnrpycError(
                    f"cannot decompile real game .rpyc {rpyc_path.name!r}: "
                    f"python_exe= was given for subprocess decoding but "
                    f"sdk_root= is missing. Either pass sdk_root= (the dir "
                    f"containing renpy/) or drop python_exe= to decode "
                    f"in-process with the vendored upstream (ADR-0008 §8.5)."
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
        # in-process path (default): upstream v2.0.4 fakes `renpy` internally
        try:
            return _decompile_in_process(rpyc_path, out_path, try_harder)
        except UnrpycError as v2_exc:
            # v2 (Ren'Py 8.x) could not decode it — try the py2.7 v1 fallback
            # (Ren'Py 7/6), then report both failures if that also fails.
            done = _decompile_v1_fallback(
                [{"src": str(rpyc_path), "dst": str(out_path)}],
                try_harder,
            )
            if done:
                return done[0]
            raise UnrpycError(
                f"real unrpyc failed to decompile {rpyc_path.name!r}: "
                f"{v2_exc}"
                + ("; Python 2.7 fallback (unrpyc v1) unavailable or failed"
                   if _v1_unrpyc_dir() is not None else "")
            ) from v2_exc

    # v2 (Ren'Py 8.x) is not vendored — try the Python 2.7 v1 fallback.
    done = _decompile_v1_fallback(
        [{"src": str(rpyc_path), "dst": str(out_path)}], try_harder
    )
    if done:
        return done[0]
    raise UnrpycError(
        f"cannot decompile {rpyc_path.name!r}: vendored unrpyc handles only "
        f"synthetic fixtures; drop the real upstream unrpyc into vendor/"
        f"unrpyc_real/ (Ren'Py 8.x) or set PYTRAN_PYTHON27 to a working "
        f"Python 2.7 for the vendored v1 tree (Ren'Py 7/6)."
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

    # Split into synthetic (verbatim) and real jobs. Real jobs are decoded
    # in-process by default; a single subprocess batch is used when the caller
    # pinned python_exe explicitly.
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
            # v2 (Ren'Py 8.x) is not vendored — fall back *entirely* to the
            # Python 2.7 v1 tree so Ren'Py 7/6 games still decode.
            recovered = _decompile_v1_fallback(
                real_jobs, try_harder, game_root=_game_root_of(rpyc_paths)
            )
            produced.extend(recovered)
            if len(recovered) < len(real_jobs):
                done = {p for p in recovered}
                missing = sorted(
                    Path(j["src"]).name for j in real_jobs
                    if Path(j["dst"]) not in done
                )
                raise UnrpycError(
                    f"cannot decompile real game .rpyc: 0/2 unrpyc versions "
                    f"could decode {len(missing)} file(s): {', '.join(missing)}. "
                    "Drop upstream v2 into vendor/unrpyc_real/ (Ren'Py 8.x), or "
                    "set PYTRAN_PYTHON27 to a working Python 2.7 for the "
                    "vendored v1 tree (Ren'Py 7/6)."
                )
            return produced
        if python_exe is not None:
            # explicit subprocess decoding (e.g. game's own bundled interpreter)
            if sdk_root is None:
                raise UnrpycError(
                    "cannot decompile real game .rpyc: python_exe= was given "
                    "but sdk_root= is missing. Either pass sdk_root= (the dir "
                    "containing renpy/) or drop python_exe= to decode "
                    "in-process with the vendored upstream (ADR-0008 §8.5)."
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
                        f"{Path(job['src']).name!r}: "
                        f"{rec.get('error') or 'unknown'}"
                    )
                produced.append(Path(job["dst"]))
        else:
            # in-process decoding (default) — upstream v2.0.4 needs no engine.
            # Files v2 cannot handle (typically Ren'Py 7/6 bytecode) are
            # collected and retried with the Python 2.7 v1 fallback.
            failed: List[dict] = []
            errors: List[str] = []
            for job in real_jobs:
                try:
                    produced.append(
                        _decompile_in_process(
                            Path(job["src"]), Path(job["dst"]), try_harder
                        )
                    )
                except UnrpycError as exc:
                    failed.append(job)
                    errors.append("%s: %s" % (Path(job["src"]).name, exc))

            if failed:
                recovered = _decompile_v1_fallback(
                    failed, try_harder, game_root=_game_root_of(rpyc_paths)
                )
                produced.extend(recovered)
                recovered_srcs = {p for p in recovered}
                still_failed = [
                    j for j in failed if Path(j["dst"]) not in recovered_srcs
                ]
                if still_failed:
                    names = ", ".join(
                        sorted(Path(j["src"]).name for j in still_failed)
                    )
                    hint = (
                        " (Ren'Py 7/6 fallback needs Python 2.7; set "
                        "PYTRAN_PYTHON27 to a working py2.7 interpreter)"
                        if _v1_unrpyc_dir() is not None and not recovered
                        else ""
                    )
                    raise UnrpycError(
                        f"real unrpyc failed to decompile {len(still_failed)} "
                        f"file(s): {names}{hint}"
                    )
    return produced


def _game_root_of(paths: List[Path]) -> Optional[Path]:
    """Best-effort game root: the ancestor that contains ``game/``."""
    for p in paths:
        cur = Path(p).resolve().parent
        for parent in [cur, *cur.parents]:
            if (parent / "game").is_dir() and (
                (parent / "renpy").is_dir() or (parent / "lib").is_dir()
            ):
                return parent
    return None


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
