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
# whitelisted alternate driver: real-C gating. The GCC16 toolchain is
# built C++-only (no cc1), so C jobs use the system compiler until the
# c,c++ rebuild ships. Request field: {"driver": "gcc"}.
SYS_GCC = os.environ.get("ORACLE_SYS_GCC", "/usr/bin/gcc")
LIB64 = os.path.join(os.path.dirname(os.path.dirname(GXX)), "lib64")
WORK = os.environ.get("ORACLE_WORKDIR", "/dev/shm/oracle")
PORT = int(os.environ.get("ORACLE_PORT", "8950"))
MAX_SOURCE = 512 * 1024          # per request, all files
MAX_OUTPUT = 256 * 1024          # stderr/stdout cap per stream
C26_ARGS = ["-std=c++26", "-freflection", "-fcontracts",
            "-fcontract-evaluation-semantic=enforce", "-Wall", "-Wextra"]

_jobs_done = 0
# Async jobs: a FORKED caller (training workers, parallel gates) submits
# with {"async": true} -> {job_id} immediately, and ANY process — the
# submitter, its fork, or a retry after a dropped connection — collects
# via GET /result/<job_id> (hs 2026-08-11: callers are fork-aware; the
# response must be fetchable over HTTP, not bound to one socket).
_async_jobs: dict = {}
_async_order: list = []
_ASYNC_KEEP = 2000
# Execution policy (hs 2026-08-11): heavier assignments MAY execute, but
# every execution gets a THROWAWAY container — never this persistent one.
# Final exec design (hs 2026-08-11): every EXEC gets a NEW container, and
# an async job_id is the caller's pointer to it — submit from one process,
# collect from any (fork-aware). Compile/link never leaves this container.
#   ORACLE_RUN_MODE=sandbox  (default) fresh sibling container per exec;
#                            falls back to inline WITH a logged downgrade
#                            if docker is unavailable
#   ORACLE_RUN_MODE=off      compile+LINK only, never execute (training)
#   ORACLE_RUN_MODE=inline   dev-only: exec in this container
RUN_MODE = os.environ.get("ORACLE_RUN_MODE",
                          "off" if os.environ.get("ORACLE_ALLOW_RUN") == "0"
                          else "sandbox")
_DOCKER_SOCK = "/var/run/docker.sock"
_DOCKER_OK = os.path.exists(_DOCKER_SOCK)


def _docker_api(method: str, path: str, body: bytes | None = None,
                ctype: str = "application/json", timeout: float = 60.0):
    """Docker Engine API over the unix socket — no docker CLI in the image
    (the CLI package costs ~400MB of deps; the API costs nothing)."""
    import http.client
    import socket as _s

    class _Conn(http.client.HTTPConnection):
        def connect(self):
            self.sock = _s.socket(_s.AF_UNIX, _s.SOCK_STREAM)
            self.sock.settimeout(timeout)
            self.sock.connect(_DOCKER_SOCK)

    c = _Conn("localhost", timeout=timeout)
    c.request(method, path, body=body, headers={"Content-Type": ctype})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


def _demux_logs(raw: bytes) -> tuple[bytes, bytes]:
    """Docker log stream: 8-byte frame headers (type, 0,0,0, len32be)."""
    outs, errs, i = [], [], 0
    while i + 8 <= len(raw):
        kind = raw[i]
        n = int.from_bytes(raw[i + 4:i + 8], "big")
        chunk = raw[i + 8:i + 8 + n]
        (errs if kind == 2 else outs).append(chunk)
        i += 8 + n
    return b"".join(outs), b"".join(errs)


def _sandbox_exec(tar_payload: bytes, timeout: float) -> dict:
    """Fresh container per exec, data passed in, result passed out —
    entirely via the Engine API. Files land at / (readonly at runtime,
    writable to the archive API pre-start; /tmp would be shadowed by its
    tmpfs mount)."""
    cfg = {
        "Image": SANDBOX_IMAGE,
        "Cmd": ["sh", "-c", "LD_LIBRARY_PATH=/ /a.out"],
        "NetworkDisabled": True,
        "HostConfig": {
            "NetworkMode": "none",
            "CapDrop": ["ALL"],
            # rootfs stays writable: the archive API refuses uploads into a
            # read-only rootfs, and this container is a per-exec throwaway —
            # anything written dies with it seconds later
            "SecurityOpt": ["no-new-privileges"],
            "Tmpfs": {"/tmp": "rw,noexec,size=67108864"},
            "Memory": 536870912,
            "PidsLimit": 64,
        },
    }
    st, data = _docker_api("POST", "/containers/create",
                           json.dumps(cfg).encode())
    if st not in (200, 201):
        return {"run_rc": -1, "run_stderr": f"sandbox create: {data[:200]!r}"}
    cid = json.loads(data)["Id"]
    try:
        st, data = _docker_api("PUT", f"/containers/{cid}/archive?path=/",
                               tar_payload, ctype="application/x-tar")
        if st != 200:
            return {"run_rc": -1, "run_stderr": f"sandbox upload: {data[:200]!r}"}
        _docker_api("POST", f"/containers/{cid}/start", b"")
        st, data = _docker_api("POST", f"/containers/{cid}/wait", b"",
                               timeout=timeout)
        if st != 200:
            return {"run_rc": -1, "run_stderr": "run timeout (sandbox)"}
        rc = json.loads(data).get("StatusCode", -1)
        _, logs = 0, b""
        st, logs = _docker_api(
            "GET", f"/containers/{cid}/logs?stdout=1&stderr=1")
        so, se = _demux_logs(logs) if st == 200 else (b"", b"")
        return {"run_rc": rc, "run_stdout": _capped(so),
                "run_stderr": _capped(se)}
    finally:
        _docker_api("DELETE", f"/containers/{cid}?force=1", None)
SANDBOX_IMAGE = os.environ.get("ORACLE_SANDBOX_IMAGE", "ubuntu:26.04")
# The oracle PASSES THE DATA to the fresh container (hs 2026-08-11): the
# binary + its 16.1 runtime stream in over stdin as a tar, unpack into the
# sandbox's own tmpfs, execute there. No shared volumes, no host-path
# coordination — the only coupling is the docker socket.
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
    driver = GXX if req.get("driver") != "gcc" else SYS_GCC
    run = bool(req.get("run"))
    link = bool(req.get("link")) or run
    degraded = run and RUN_MODE == "off"
    if degraded:
        # training grades by compile+LINK (hs: "you can of course link,
        # but not run") — degrade the job rather than refuse it
        run, link = False, True
    timeout = min(float(req.get("timeout") or 60), 300.0)

    d = tempfile.mkdtemp(prefix="job-", dir=WORK)
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
        argv = [driver, *[str(a) for a in args]]
        if link:
            argv += [os.path.basename(main), "-o", "a.out"]
        else:
            argv += ["-fsyntax-only", os.path.basename(main)]
        try:
            p = subprocess.run(argv, cwd=d, env=env, capture_output=True,
                               timeout=timeout)
        except subprocess.TimeoutExpired as te:
            # partial streams matter: a timeout mid-cascade still shows
            # WHICH error the compiler was drowning in
            return {"ok": False, "rc": -1,
                    "error": f"compile timeout {timeout}s",
                    "stdout": _capped(te.stdout or b""),
                    "stderr": _capped(te.stderr or b""),
                    "ms": round((time.monotonic() - t0) * 1000)}
        out = {"rc": p.returncode, "stdout": _capped(p.stdout),
               "stderr": _capped(p.stderr)}
        ok = p.returncode == 0
        if ok and run:
            if RUN_MODE == "sandbox" and _DOCKER_OK:
                import io
                import tarfile
                buf = io.BytesIO()
                with tarfile.open(fileobj=buf, mode="w") as tf:
                    tf.add(os.path.join(d, "a.out"), arcname="a.out")
                    for lib in ("libstdc++.so.6", "libgcc_s.so.1"):
                        src = os.path.join(LIB64, lib)
                        if os.path.exists(src):
                            tf.add(src, arcname=lib)
                res = _sandbox_exec(buf.getvalue(), max(5.0, timeout / 2))
                out.update(**res, run_mode=RUN_MODE)
                ok = res.get("run_rc") == 0
            else:
                if RUN_MODE == "sandbox":
                    out["run_note"] = ("sandbox requested but no docker "
                                       "socket: ran inline")
                try:
                    r = subprocess.run(["./a.out"], cwd=d, env=env,
                                       capture_output=True,
                                       timeout=max(5.0, timeout / 2))
                    out.update(run_rc=r.returncode,
                               run_stdout=_capped(r.stdout),
                               run_stderr=_capped(r.stderr),
                               run_mode="inline")
                    ok = r.returncode == 0
                except subprocess.TimeoutExpired as te:
                    out.update(run_rc=-1,
                               run_stdout=_capped(te.stdout or b""),
                               run_stderr=_capped(te.stderr or b"")
                               + "\n[oracle] run timeout",
                               run_mode="inline")
                    ok = False
        if degraded:
            out["run_degraded"] = ("executed nothing: ORACLE_RUN_MODE=off "
                                   "grades compile+link only")
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


def _submit_async(req: dict) -> str:
    import uuid
    jid = uuid.uuid4().hex[:16]
    _async_jobs[jid] = None                     # pending
    _async_order.append(jid)
    while len(_async_order) > _ASYNC_KEEP:
        _async_jobs.pop(_async_order.pop(0), None)
    def work():
        _async_jobs[jid] = compile_job(req)
    threading.Thread(target=work, daemon=True).start()
    return jid


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
        elif self.path.startswith("/result/"):
            jid = self.path.rsplit("/", 1)[-1]
            if jid not in _async_jobs:
                self._send(404, {"error": f"unknown job {jid}"})
            elif _async_jobs[jid] is None:
                self._send(202, {"job_id": jid, "pending": True})
            else:
                self._send(200, _async_jobs[jid])
        else:
            self._send(404, {"error": "GET /health|/result/<id> or POST /compile"})

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
        if req.get("async"):
            self._send(202, {"job_id": _submit_async(req)})
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
        if os.environ.get("ORACLE_ALLOW_NO_REFLECTION") == "1":
            print("[oracle] serving WITHOUT reflection (special-purpose "
                  "instance; health reports reflection:false)", flush=True)
        else:
            raise SystemExit("REFUSING to serve: reflection probe failed")
    Server(("0.0.0.0", PORT), Handler).serve_forever()
