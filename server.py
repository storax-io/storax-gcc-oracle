#!/usr/bin/env python3
"""g++ 16.1 oracle — a fast compile-job RPC around the platform's
authoritative C++26 toolchain.

The ONE job of this service: answer "does this code compile (and run)?"
with the exact compiler that owns the platform's C++26 truth — reflection,
contracts, the final std::meta surface — as fast as a network round-trip
allows.

Speed decisions (this is a hot gate, not a build farm):
  * stdlib-only threaded HTTP with keep-alive — no framework, ~0 import
    cost, one connection can stream many jobs;
  * every job compiles in /dev/shm (tmpfs) — no disk in the path;
  * the compiler stays warm in the page cache (cc1plus is ~45MB stripped;
    after the first job it never leaves RAM);
  * jobs are independent processes — 16 cores = 16 parallel jobs, no lock.

Protocol (JSON over HTTP):
  GET  /health   -> {ok, version, reflection, jobs_done}
  POST /compile  -> body: {
        "files":   {"name.cpp": "source", ...}     (required)
        "args":    ["-std=c++26", ...]             (default: C26_ARGS)
        "main":    "name.cpp"                      (default: sole/first file)
        "run":     false                           (also execute a.out)
        "timeout": 60                              (seconds, compile+run)
     } -> {ok, rc, stdout, stderr, ms, run_rc, run_stdout, run_stderr}

`ok` is rc==0 (and run_rc==0 when run=true). stderr is byte-capped: the
platform learned the hard way that a single template cascade can emit
hundreds of MB (2026-08-08)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GXX = os.environ.get("ORACLE_GXX", "/opt/gcc-16.1/bin/g++")
LIB64 = os.path.join(os.path.dirname(os.path.dirname(GXX)), "lib64")
WORK = os.environ.get("ORACLE_WORKDIR", "/dev/shm/oracle")
PORT = int(os.environ.get("ORACLE_PORT", "8950"))
MAX_SOURCE = 512 * 1024          # per request, all files
MAX_OUTPUT = 256 * 1024          # stderr/stdout cap per stream
C26_ARGS = ["-std=c++26", "-freflection", "-fcontracts",
            "-fcontract-evaluation-semantic=enforce", "-Wall", "-Wextra"]

_jobs_done = 0
# Execution policy (hs 2026-08-11): heavier assignments MAY execute, but
# every execution gets a THROWAWAY container — never this persistent one.
#   ORACLE_RUN_MODE=inline   (default) fork, execute in THIS container,
#                            delete the job dir — the container is the
#                            boundary (hs 2026-08-11: "just fork, execute
#                            and delete")
#   ORACLE_RUN_MODE=off      compile+LINK only, never execute (training)
#   ORACLE_RUN_MODE=sandbox  per-exec sibling container (kept for future
#                            hostile-input scenarios; needs docker.sock)
RUN_MODE = os.environ.get("ORACLE_RUN_MODE",
                          "off" if os.environ.get("ORACLE_ALLOW_RUN") == "0"
                          else "inline")
SANDBOX_IMAGE = os.environ.get("ORACLE_SANDBOX_IMAGE", "ubuntu:26.04")
# Sibling containers mount HOST paths: exec jobs build here (bind-mounted),
# not in the oracle's private /dev/shm, and the container/host path pair
# must be configured together.
JOBS_DIR = os.environ.get("ORACLE_JOBS_DIR", "/jobs")
JOBS_HOST = os.environ.get("ORACLE_JOBS_HOST", "/home/hs/oracle-jobs")
# GIANT PARALLELISM contract (hs 2026-08-10): callers may throw hundreds of
# jobs at once (training rewards, batch gates). Every HTTP thread is cheap;
# the COMPILES are bounded to the core count — excess jobs queue on the
# semaphore instead of fork-bombing the box. Horizontal scale = more
# containers on more hosts; the client shards.
MAX_PARALLEL = int(os.environ.get("ORACLE_MAX_PARALLEL",
                                  str(os.cpu_count() or 4)))
_slots = threading.BoundedSemaphore(MAX_PARALLEL)
_inflight = 0
_inflight_lock = threading.Lock()


def _capped(b: bytes) -> str:
    if len(b) <= MAX_OUTPUT:
        return b.decode(errors="replace")
    return (b[:MAX_OUTPUT].decode(errors="replace")
            + f"\n[oracle] output truncated at {MAX_OUTPUT} bytes")


def compile_job(req: dict) -> dict:
    global _jobs_done, _inflight
    files = req.get("files") or {}
    if not files or not isinstance(files, dict):
        return {"ok": False, "error": "need files: {name: source}"}
    if sum(len(str(v)) for v in files.values()) > MAX_SOURCE:
        return {"ok": False, "error": f"source exceeds {MAX_SOURCE} bytes"}
    args = req.get("args") or C26_ARGS
    main = req.get("main") or (next(iter(files)) if len(files) == 1 else None)
    if main is None or main not in files:
        return {"ok": False, "error": "multi-file job needs main: <name>"}
    run = bool(req.get("run"))
    link = bool(req.get("link")) or run
    if run and RUN_MODE == "off":
        # training grades by compile+LINK (hs: "you can of course link,
        # but not run") — degrade the job rather than refuse it
        run, link = False, True
    timeout = min(float(req.get("timeout") or 60), 300.0)

    workdir = JOBS_DIR if (run and RUN_MODE == "sandbox") else WORK
    os.makedirs(workdir, exist_ok=True)
    d = tempfile.mkdtemp(prefix="job-", dir=workdir)
    env = {"PATH": "/usr/bin:/bin",
           "LD_LIBRARY_PATH": LIB64}
    with _inflight_lock:
        _inflight += 1
    _slots.acquire()
    try:
        for name, src in files.items():
            safe = os.path.basename(str(name))
            with open(os.path.join(d, safe), "w") as f:
                f.write(str(src))
        t0 = time.monotonic()
        argv = [GXX, *[str(a) for a in args]]
        if link:
            argv += [os.path.basename(main), "-o", "a.out"]
        else:
            argv += ["-fsyntax-only", os.path.basename(main)]
        try:
            p = subprocess.run(argv, cwd=d, env=env, capture_output=True,
                               timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"ok": False, "rc": -1, "error": f"compile timeout {timeout}s",
                    "ms": round((time.monotonic() - t0) * 1000)}
        out = {"rc": p.returncode, "stdout": _capped(p.stdout),
               "stderr": _capped(p.stderr)}
        ok = p.returncode == 0
        if ok and run:
            if RUN_MODE == "sandbox":
                # the throwaway needs the 16.1 runtime: system libstdc++ in
                # the base image is OLDER than the compiler's — ship ours
                # next to the binary and mount the single job dir
                for lib in ("libstdc++.so.6", "libgcc_s.so.1"):
                    src = os.path.join(LIB64, lib)
                    if os.path.exists(src):
                        shutil.copy2(src, d)
                host_job = os.path.join(JOBS_HOST, os.path.basename(d))
                argv2 = ["docker", "run", "--rm",
                         "--network", "none",
                         "--cap-drop", "ALL",
                         "--security-opt", "no-new-privileges",
                         "--read-only",
                         "--tmpfs", "/tmp:rw,noexec,size=64m",
                         "--memory", "512m", "--pids-limit", "64",
                         "-v", f"{host_job}:/job:ro",
                         "-e", "LD_LIBRARY_PATH=/job",
                         SANDBOX_IMAGE, "/job/a.out"]
            else:
                argv2 = ["./a.out"]
            try:
                r = subprocess.run(argv2, cwd=d, env=env,
                                   capture_output=True,
                                   timeout=max(5.0, timeout / 2))
                out.update(run_rc=r.returncode,
                           run_stdout=_capped(r.stdout),
                           run_stderr=_capped(r.stderr),
                           run_mode=RUN_MODE)
                ok = r.returncode == 0
            except subprocess.TimeoutExpired:
                out.update(run_rc=-1, run_stderr="run timeout")
                ok = False
        out["ok"] = ok
        out["ms"] = round((time.monotonic() - t0) * 1000)
        _jobs_done += 1
        return out
    finally:
        _slots.release()
        with _inflight_lock:
            _inflight -= 1
        shutil.rmtree(d, ignore_errors=True)


_REFLECTION_PROBE = """#include <meta>
static_assert(^^int == ^^int);           // reflections compare
constexpr auto r = ^^int;
using T = [:r:];                          // splice back to the type
static_assert(__is_same(T, int));
int main() {}
"""


def health() -> dict:
    ver = subprocess.run([GXX, "--version"], capture_output=True, text=True,
                         env={"LD_LIBRARY_PATH": LIB64})
    r = compile_job({"files": {"probe.cpp": _REFLECTION_PROBE}})
    return {"ok": bool(r.get("ok")),
            "version": (ver.stdout.splitlines() or ["?"])[0],
            "reflection": bool(r.get("ok")),
            "probe_ms": r.get("ms"),
            "run_mode": RUN_MODE,
            "max_parallel": MAX_PARALLEL,
            "inflight": _inflight,
            "jobs_done": _jobs_done}


class Server(ThreadingHTTPServer):
    # socketserver's default listen backlog is 5 — a 64-connection burst
    # over the LAN gets RST at accept (measured 2026-08-10). Giant
    # parallelism means giant accept bursts.
    request_queue_size = 256


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"          # keep-alive: many jobs, one socket

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, health())
        else:
            self._send(404, {"error": "GET /health or POST /compile"})

    def do_POST(self):
        if self.path != "/compile":
            self._send(404, {"error": "POST /compile"})
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n))
        except (ValueError, json.JSONDecodeError):
            self._send(400, {"ok": False, "error": "bad JSON"})
            return
        res = compile_job(req)
        self._send(200 if "error" not in res else 400, res)

    def log_message(self, *a):              # stdout noise costs latency
        pass


if __name__ == "__main__":
    os.makedirs(WORK, exist_ok=True)
    print(f"g++ oracle on :{PORT}  compiler={GXX}  work={WORK}", flush=True)
    h = health()
    print(f"health: {h}", flush=True)
    if not h["reflection"]:
        raise SystemExit("REFUSING to serve: reflection probe failed")
    Server(("0.0.0.0", PORT), Handler).serve_forever()
