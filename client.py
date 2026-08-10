#!/usr/bin/env python3
"""Thin client for the g++ oracle. Library + CLI.

    python3 client.py health
    python3 client.py compile file.cpp [--run] [--std c++26]
"""
from __future__ import annotations

import json
import sys
import urllib.request

DEFAULT = __import__("os").environ.get("ORACLE_URL", "http://localhost:8950")


def rpc(path: str, body: dict | None = None, base: str = DEFAULT,
        timeout: float = 330.0) -> dict:
    req = urllib.request.Request(
        f"{base.rstrip('/')}{path}",
        json.dumps(body).encode() if body is not None else None,
        {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def compile_src(src: str, *, run: bool = False, args: list[str] | None = None,
                base: str = DEFAULT) -> dict:
    body: dict = {"files": {"main.cpp": src}, "run": run}
    if args:
        body["args"] = args
    return rpc("/compile", body, base)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["health", "compile"])
    ap.add_argument("file", nargs="?")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--base", default=DEFAULT)
    a = ap.parse_args()
    if a.cmd == "health":
        print(json.dumps(rpc("/health", base=a.base), indent=1))
    else:
        r = compile_src(open(a.file).read(), run=a.run, base=a.base)
        print(json.dumps(r, indent=1)[:2000])
