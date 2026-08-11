# storax-gcc-oracle

A minimal, fast compile-job RPC around GCC 16.1 — the C++26 oracle
(**reflection + contracts**) for the [storax](https://storax.ai) agentic
coding platform. One question, answered as fast as a network hop allows:
**does this code compile (and run)?**

Container-only by design, built for **giant parallelism**: training-reward
loops and batch verification gates throw hundreds of jobs at it; each
container bounds concurrent compiles to its cores and queues the rest,
and scale-out is more containers on more hosts with client-side sharding.

## Numbers (measured 2026-08-10, 16-core Zen 5, LAN client)

| | |
| --- | --- |
| trivial compile | 4 ms |
| reflection compile + execute | ~200 ms (the `<meta>` include dominates) |
| round-trip median | 20 ms |
| 256 jobs from 64 client threads | 0.2 s total — ~1 ms/job effective |
| image | 402 MB (toolchain pruned 2.0 GB → 103 MB) |

## API

    GET  /health   -> {ok, version, reflection, probe_ms,
                       max_parallel, inflight, jobs_done}
    POST /compile  -> {"files": {"main.cpp": "..."},   required
                       "args":  [...],                 default: C++26 set
                       "main":  "main.cpp",            for multi-file jobs
                       "run":   false,                 also execute a.out
                       "timeout": 60}
                   -> {ok, rc, stdout, stderr, ms, run_rc, run_stdout, ...}

Default flags: `-std=c++26 -freflection -fcontracts
-fcontract-evaluation-semantic=enforce -Wall -Wextra`; `-fsyntax-only`
unless `run` is set. stdout/stderr are byte-capped — a single template
cascade can emit hundreds of megabytes, and the platform has the scars.

## Architecture contract (2026-08-11)

**The oracle is oracle-only** — never colocated with a trainer or any
other workload. The trainer is an HTTP client; the judge is this sealed
container. Size budget: **under 500 MB** (currently 436 MB — enforced by
talking to the Docker Engine API over the socket instead of shipping the
~400 MB docker-cli package).

Execution model: compile/link happen in the oracle; every **exec** gets a
**fresh sibling container** — the binary and its 16.1 runtime stream in
via the Engine archive API (no shared volumes, no host paths), output
streams back, the container is destroyed. Callers are fork-aware: submit
with `"async": true` → `{job_id}` is the pointer; ANY process collects at
`GET /result/<job_id>`. Run modes: `sandbox` (default), `off`
(training: compile+link, never execute), `inline` (dev only).

## Design

- **Exact toolchain identity.** The binaries are the platform's
  authoritative GCC 16.1 build, pruned to the C++ compile path
  (`fetch-toolchain.sh`; keep `libstdc++exp.a` — reflection and contracts
  link against it). The base image matches the build host's distro, so
  glibc compatibility holds by construction.
- **A broken oracle cannot exist.** The image build fails unless a real
  reflection program compiles; the server refuses to start unless the
  probe passes at runtime.
- **Nothing in the hot path but the compiler.** Stdlib-only threaded HTTP
  with keep-alive; jobs in `/dev/shm` (mounted `exec` — docker tmpfs is
  noexec by default); a 256-deep accept backlog for connection bursts;
  compile slots bounded by `ORACLE_MAX_PARALLEL` (default: core count).

## Run

    ./fetch-toolchain.sh          # ORACLE_TOOLCHAIN_HOST=<host-with-gcc-16.1>
    docker compose up --build -d
    python3 tests/smoke.py http://localhost:8950

Or ship the prebuilt image: `docker save storax-gxx-oracle | gzip` to the
target and use `run-container.sh` (compose-free).

Client configuration (flag > env > default): `--url` / `ORACLE_URL` takes
one URL or a comma-separated list — with a list, jobs round-robin across
oracles (client-side sharding is the scale-out mechanism);
`--timeout` / `ORACLE_TIMEOUT_S`.

## Consumers

- storax enhancement gates (`STORAX_GATE_ORACLE_URL`) — compiler-verified
  corpus notes for C++26, gated from any host
- the C++26 continued-training project — compiler-as-reward at reward-loop
  throughput
- validation stages for reflection-family benchmark instances

---
Built for the storax platform. Licensed Apache-2.0 (this component). Developed with AI assistance
(Claude); all design decisions and commits by the human maintainer.
