#!/usr/bin/env bash
# Regenerate toolchain/ from virre's /opt/gcc-16.1 (the authoritative
# build). Prunes to the C++ compile path and strips: 2.0GB -> ~103MB.
set -euo pipefail
cd "$(dirname "$0")"
rsync -a --delete \
  --include='/bin/' --include='/bin/g++' --include='/bin/gcc' \
  --include='/libexec/***' --include='/lib/***' --include='/lib64/***' \
  --include='/include/***' --exclude='*' \
  "${ORACLE_TOOLCHAIN_HOST:?set ORACLE_TOOLCHAIN_HOST=<host-with-gcc-16.1>}":/opt/gcc-16.1/ toolchain/
python3 - <<'PY'
import subprocess
from pathlib import Path
t = Path('toolchain')
lx = t/'libexec/gcc/x86_64-pc-linux-gnu/16.1.0'
for n in ('cc1','f951','lto1','lto-wrapper','g++-mapper-server'):
    p = lx/n
    if p.exists(): p.unlink()
for p in list(t.rglob('*.a')):
    if p.name not in ('libgcc.a','libgcc_eh.a','libstdc++exp.a'): p.unlink()
for p in list(t.rglob('*.la')):
    p.unlink()
for p in list((t/'bin').iterdir()):
    if p.name not in ('g++','gcc'): p.unlink()
for p in list(t.rglob('*')):
    if p.is_file() and (p.name in ('cc1plus','collect2','g++','gcc')
                        or '.so' in p.name):
        subprocess.run(['strip','--strip-unneeded',str(p)],
                       capture_output=True)
subprocess.run(['du','-sh',str(t)])
PY
