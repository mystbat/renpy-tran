# -*- coding: utf-8 -*-
"""Subprocess worker for upstream unrpyc **v1** (Ren'Py 7.x / 6.x fallback).

This is the Python-2.7 counterpart of ``vendor/unrpyc_real/runner.py``. It must
run under **Python 2.7** because the vendored v1 sources are Python 2
(``except Exception, e:``) and upstream's ``main()`` rejects any other version.

Invoked by :mod:`vendor.unrpyc` when v2 cannot decode a file (typically a
Ren'Py 7/6 game). It:

1. Puts the v1 vendored tree on ``sys.path``.
2. Decodes each ``.rpyc`` via :func:`vendor.unrpyc_real_v1.decompile_file`.
3. Emits one JSON line of ``{"src", "dst", "ok", "error"}`` on stdout.

CLI::

    python2 worker_v1.py [--jobs jobs.json]
                         [src dst [src dst ...]]

``--jobs`` is a JSON list of ``{"src","dst"}`` objects, or ``-`` to read from
stdin.
"""

from __future__ import print_function

import json
import os
import sys

# runner_v1.py -> unrpyc_real_v1 -> vendor -> <repo root>
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
_V1_DIR = os.path.dirname(os.path.abspath(__file__))

for _p in (_REPO_ROOT, _V1_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _read_jobs(argv):
    """Return a list of {'src','dst'} from --jobs / positional pairs."""
    jobs = []
    extra = []
    stdin_jobs = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--jobs":
            stdin_jobs = argv[i + 1] == "-"
            if not stdin_jobs:
                with open(argv[i + 1], "r") as f:
                    for item in json.load(f):
                        jobs.append({"src": item["src"], "dst": item["dst"]})
            i += 2
            continue
        if a in ("--try-harder",):
            i += 1
            continue
        extra.append(a)
        i += 1

    if stdin_jobs:
        data = json.loads(sys.stdin.read())
        for item in data:
            jobs.append({"src": item["src"], "dst": item["dst"]})

    it = iter(extra)
    for src in it:
        dst = next(it, None)
        if dst is None:
            raise ValueError("positional args must come in src dst pairs")
        jobs.append({"src": src, "dst": dst})
    return jobs


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    try_harder = "--try-harder" in argv
    try:
        jobs = _read_jobs(argv)
    except Exception as exc:  # noqa: BLE001
        sys.stdout.write(json.dumps({"error": str(exc)}) + "\n")
        return 2
    if not jobs:
        sys.stdout.write(json.dumps({"error": "no jobs given"}) + "\n")
        return 2

    # Import the upstream v1 module directly. We deliberately avoid the `vendor`
    # package (its __init__.py uses non-ASCII literals and is therefore not
    # importable under Python 2.7); V1_DIR is already on sys.path.
    import tempfile
    import shutil

    import unrpyc  # noqa: E402

    results = []
    for job in jobs:
        rec = {"src": job["src"], "dst": job["dst"], "ok": False, "error": None}
        try:
            src_path = job["src"]
            dst_path = job["dst"]
            dst_dir = os.path.dirname(dst_path) or "."
            if not os.path.isdir(dst_dir):
                os.makedirs(dst_dir)

            # Stage beside dst (no mkdir) when writable, else a temp dir. This
            # mirrors decompile_file()'s behaviour in-process.
            stage_name = "%s.unrpyc_stage%s" % (
                os.path.splitext(os.path.basename(src_path))[0],
                os.path.splitext(src_path)[1],
            )
            target = os.path.join(dst_dir, stage_name)
            try:
                probe = os.path.join(dst_dir, ".unrpyc_write_probe")
                with open(probe, "wb") as f:
                    f.write(b"")
                os.unlink(probe)
                writable = True
            except OSError:
                writable = False
            td = None
            if not writable:
                base = os.environ.get("UNRPYC_WORK_DIR") or None
                td = tempfile.mkdtemp(prefix="unrpyc_v1_work_", dir=base)
                target = os.path.join(td, stage_name)
            try:
                with open(src_path, "rb") as f:
                    data = f.read()
                with open(target, "wb") as f:
                    f.write(data)
                ctx = unrpyc.Context()
                unrpyc.decompile_rpyc(
                    target, ctx, overwrite=True,
                    init_offset=True, try_harder=try_harder,
                )
                if ctx.error is not None:
                    raise ctx.error
                produced = os.path.splitext(target)[0] + ".rpy"
                if not os.path.exists(produced):
                    raise RuntimeError("no .rpy produced")
                with open(produced, "rb") as f:
                    data = f.read()
                with open(dst_path, "wb") as f:
                    f.write(data)
                rec["ok"] = os.path.exists(dst_path) and os.path.getsize(dst_path) > 0
            finally:
                for p in (target, os.path.splitext(target)[0] + ".rpy"):
                    try:
                        if os.path.exists(p):
                            os.unlink(p)
                    except OSError:
                        pass
                if td is not None:
                    shutil.rmtree(td, ignore_errors=True)
        except Exception as exc:  # noqa: BLE001
            rec["error"] = "%s: %s" % (type(exc).__name__, exc)
        results.append(rec)

    sys.stdout.write(json.dumps(results, ensure_ascii=False) + "\n")
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
