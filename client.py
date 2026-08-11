#!/usr/bin/env python3
"""Client for the g++ oracle — library + CLI, shard-aware.

Configuration (flag > env > default):
    --url / ORACLE_URL      one URL, or comma-separated URLs for sharding
    --timeout / ORACLE_TIMEOUT_S

With several URLs the client round-robins jobs across oracles — the
intended scale-out shape: each container saturates one host's cores;
parallelism beyond that is more hosts, sharded here on the client side.

    python3 client.py health
    python3 client.py compile file.cpp [--run] [--url http://host:8950]
"""
from __future__ import annotations

import itertools
import json
import os
import urllib.request

DEFAULT_URLS = os.environ.get("ORACLE_URL", "http://localhost:8950")
DEFAULT_TIMEOUT = float(os.environ.get("ORACLE_TIMEOUT_S", "330"))


class Oracle:
    """One or many oracle endpoints; jobs round-robin across them."""

    def __init__(self, urls: str | list[str] = DEFAULT_URLS,
                 timeout: float = DEFAULT_TIMEOUT) -> None:
        if isinstance(urls, str):
            urls = [u.strip() for u in urls.split(",") if u.strip()]
        if not urls:
            raise ValueError("no oracle URLs configured")
        self.urls = [u.rstrip("/") for u in urls]
        self.timeout = timeout
        self._rr = itertools.cycle(self.urls)

    def _rpc(self, base: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(
            f"{base}{path}",
            json.dumps(body).encode() if body is not None else None,
            {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read())

    def health(self) -> list[dict]:
        return [{"url": u, **self._rpc(u, "/health")} for u in self.urls]

    def compile(self, files: dict[str, str] | str, *, run: bool = False,
                args: list[str] | None = None, main: str | None = None,
                timeout: float | None = None) -> dict:
        if isinstance(files, str):
            files = {"main.cpp": files}
        body: dict = {"files": files, "run": run}
        if args:
            body["args"] = args
        if main:
            body["main"] = main
        if timeout:
            body["timeout"] = timeout
        return self._rpc(next(self._rr), "/compile", body)


def submit(files, *, run=False, args=None, base: str = "") -> tuple[str, str]:
    """Fire a job; returns (base_url, job_id). Collect from ANY process."""
    o = Oracle(base or DEFAULT_URLS)
    url = next(o._rr)
    body = {"files": files if isinstance(files, dict) else {"main.cpp": files},
            "run": run, "async": True}
    if args:
        body["args"] = args
    return url, o._rpc(url, "/compile", body)["job_id"]


def collect(url: str, job_id: str, *, wait: float = 300.0,
            poll: float = 0.5) -> dict:
    """Fetch an async result — usable from a fork, a retry, anywhere."""
    import time as _t
    o = Oracle(url)
    deadline = _t.monotonic() + wait
    while True:
        r = o._rpc(url, f"/result/{job_id}")
        if not r.get("pending"):
            return r
        if _t.monotonic() > deadline:
            raise TimeoutError(f"job {job_id} still pending after {wait}s")
        _t.sleep(poll)


def compile_src(src: str, *, run: bool = False, args: list[str] | None = None,
                base: str = "") -> dict:
    """Back-compat one-shot helper (base: single URL or empty for env)."""
    return Oracle(base or DEFAULT_URLS).compile(src, run=run, args=args)


def rpc(path: str, body: dict | None = None, base: str = "",
        timeout: float = DEFAULT_TIMEOUT) -> dict:
    o = Oracle(base or DEFAULT_URLS, timeout)
    return o._rpc(o.urls[0], path, body)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["health", "compile"])
    ap.add_argument("file", nargs="?")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--url", default=DEFAULT_URLS,
                    help="oracle URL(s), comma-separated for sharding "
                         "(env: ORACLE_URL)")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    ap.add_argument("--std", default="",
                    help="override standard, e.g. c++23")
    a = ap.parse_args()
    o = Oracle(a.url, a.timeout)
    if a.cmd == "health":
        print(json.dumps(o.health(), indent=1))
    else:
        args = None
        if a.std:
            args = [f"-std={a.std}", "-Wall", "-Wextra"]
        r = o.compile(open(a.file).read(), run=a.run, args=args)
        print(json.dumps(r, indent=1)[:2000])
