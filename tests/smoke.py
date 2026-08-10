#!/usr/bin/env python3
"""Oracle smoke: correctness + the latency numbers the design promises."""
import concurrent.futures as cf
import statistics
import sys
import time

sys.path.insert(0, __import__('os').path.dirname(__import__('os').path.dirname(__import__('os').path.abspath(__file__))))
from client import compile_src, rpc

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8950"

h = rpc("/health", base=BASE)
assert h["ok"] and h["reflection"], h
print(f"[ok] health: {h['version']}  reflection probe {h['probe_ms']}ms")

r = compile_src("int main(){}", base=BASE)
assert r["ok"], r
print(f"[ok] trivial compile {r['ms']}ms")

REFL = """#include <meta>
#include <cstdio>
struct Point { int x; int y; };
// the vector<info> must not OUTLIVE constant evaluation (transient
// allocation rule) — consume it inside the constant expression
constexpr auto n = std::meta::nonstatic_data_members_of(
    ^^Point, std::meta::access_context::current()).size();
static_assert(n == 2);
int main() {
    std::printf("%zu\\n", n);
    return 0;
}
"""
r = compile_src(REFL, run=True, base=BASE)
assert r["ok"] and r.get("run_stdout", "").strip() == "2", r
print(f"[ok] reflection compile+run {r['ms']}ms (members_of works)")

r = compile_src("int main(){ return broken; }", base=BASE)
assert not r["ok"] and "broken" in r["stderr"], r
print("[ok] failing code reports rc!=0 with diagnostic")

lat = []
for _ in range(10):
    t0 = time.monotonic()
    assert compile_src("int main(){}", base=BASE)["ok"]
    lat.append((time.monotonic() - t0) * 1000)
print(f"[ok] sequential round-trip: median {statistics.median(lat):.0f}ms "
      f"min {min(lat):.0f}ms")

t0 = time.monotonic()
with cf.ThreadPoolExecutor(64) as ex:
    rs = list(ex.map(lambda _: compile_src("int main(){}", base=BASE)["ok"],
                     range(256)))
wall = time.monotonic() - t0
assert all(rs)
print(f"[ok] GIANT: 256 jobs, 64 client threads: {wall:.1f}s "
      f"({wall/256*1000:.0f}ms/job effective; server bounds compiles to "
      f"its cores, excess queues)")
print("ALL ORACLE CHECKS PASSED")
