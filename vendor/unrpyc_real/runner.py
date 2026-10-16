#!/usr/bin/env python3
"""Subprocess worker for the REAL upstream unrpyc (design §8.5 SDK-bridge).

This module can be spawned as a **separate process** by ``vendor.unrpyc`` when
the caller wants process isolation — for example to decode under the game's own
bundled Python. It is no longer strictly required: unlike the previously
vendored 2019 upstream, unrpyc v2 does **not** import a real ``renpy`` engine
(it installs a fake ``renpy`` package itself via
``decompiler/renpycompat.py``), so :func:`vendor.unrpyc_real.decompile_file`
can also be called directly in-process on Python ≥ 3.9.

The worker:

1. Puts the Ren'Py engine (``--sdk-root``) on ``sys.path`` **if given**, for
   games whose nodes carry a customised pickle layout. Upstream v2 does not
   need it, so the flag is optional now.
2. Puts the project root on ``sys.path`` (self-located) so the vendored
   ``vendor.unrpyc_real`` package is importable.
3. Decodes each ``.rpyc`` to ``.rpy`` via :func:`vendor.unrpyc_real.decompile_file`.
4. Emits a single JSON line describing every job's result on stdout, so the
   parent can parse it deterministically.

CLI::

    python runner.py --sdk-root /path/to/renpy-engine \\
        [--try-harder] [--jobs jobs.json] [src dst [src dst ...]]

``jobs.json`` is a JSON list of objects ``{"src": "...", "dst": "..."}``. When
``--jobs`` is omitted, positional ``src dst`` pairs are used. The result is a
JSON array of ``{"src", "dst", "ok", "error"}`` printed to stdout (and nothing
else, so the parent can parse it without interference).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List


def _repo_root() -> Path:
    # runner.py -> unrpyc_real -> vendor -> <repo root>
    return Path(__file__).resolve().parents[2]


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Real unrpyc subprocess worker")
    p.add_argument("--sdk-root", default=None, required=False,
                   help="Optional directory containing a Ren'Py 'renpy/' "
                        "engine (added to sys.path). Upstream v2 does not "
                        "require a real engine; supply this only for games "
                        "needing one.")
    p.add_argument("--try-harder", action="store_true",
                   help="Use the obfuscation-recovery path (upstream "
                        "--try-harder). Default off.")
    p.add_argument("--jobs", default=None,
                   help="Path to a JSON file: list of {'src','dst'} objects. "
                        "Use '-' to read JSON from stdin.")
    return p


def _read_jobs(args: argparse.Namespace, extra_pairs: List[str]) -> List[Dict[str, str]]:
    jobs: List[Dict[str, str]] = []
    if args.jobs:
        raw = sys.stdin.read() if args.jobs == "-" else Path(args.jobs).read_text(encoding="utf-8")
        data = json.loads(raw)
        for item in data:
            jobs.append({"src": str(item["src"]), "dst": str(item["dst"])})
    # positional src dst pairs
    it = iter(extra_pairs)
    for src in it:
        dst = next(it, None)
        if dst is None:
            raise SystemExit("positional args must come in src dst pairs")
        jobs.append({"src": src, "dst": dst})
    if not jobs:
        raise SystemExit("no jobs given (use --jobs or positional src dst pairs)")
    return jobs


def main(argv: List[str] | None = None) -> int:
    parser = _build_parser()
    # split known args from the trailing positional src/dst pairs
    args, extra = parser.parse_known_args(argv)
    try:
        jobs = _read_jobs(args, extra)
    except SystemExit as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2

    # 1) Ren'Py engine on path, when one was supplied. Upstream v2 reconstructs
    #    the AST itself and does not need a real engine, so this is optional.
    if args.sdk_root:
        sdk_root = str(Path(args.sdk_root).resolve())
        if sdk_root not in sys.path:
            sys.path.insert(0, sdk_root)

    # 2) project root so `import vendor.unrpyc_real` works
    root = str(_repo_root())
    if root not in sys.path:
        sys.path.insert(0, root)

    from vendor.unrpyc_real import decompile_file  # noqa: E402

    results: List[Dict[str, object]] = []
    for job in jobs:
        src, dst = job["src"], job["dst"]
        rec: Dict[str, object] = {"src": src, "dst": dst, "ok": False, "error": None}
        try:
            out = decompile_file(src, dst, try_harder=args.try_harder)
            rec["ok"] = out is not None and Path(dst).exists()
        except Exception as exc:  # noqa: BLE001 - report any decode failure
            rec["error"] = f"{type(exc).__name__}: {exc}"
        results.append(rec)

    # 3) emit exactly one JSON line for the parent to parse
    sys.stdout.write(json.dumps(results, ensure_ascii=False) + "\n")
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
