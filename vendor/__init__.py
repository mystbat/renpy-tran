"""vendor package — bundled third-party tooling kept as auditable source.

Per ADR-0005 the upstream unrpyc (CensoredUsername + F95Sam) and rpatool
(Shizmob) live here as plaintext source so they can be reviewed, licensed
and unit-tested. W4 vendors a faithful, stdlib-only re-implementation of the
RPA-3.0 pack/extract format, plus a pragmatic decompile entry point that
works against the synthetic fixtures and dispatches to the real upstream
unrpyc when it is later dropped in. See vendor/README.md for attribution.
"""
