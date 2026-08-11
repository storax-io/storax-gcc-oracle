# g++ 16.1 oracle — minimal container around the platform's authoritative
# C++26 compiler (reflection + contracts).
#
# The toolchain/ tree is the PRUNED virre build: 2.0GB -> 103MB (C++ path
# only — cc1plus/collect2 stripped, no cc1/lto1/fortran, .so + libgcc.a
# only). fetch-toolchain.sh regenerates it from virre. Base is ubuntu:26.04
# = virre's own distro, so the binaries run on the exact glibc they were
# built against.
#
# Speed: image ~220MB total; cc1plus (~45MB) lives in page cache after the
# first job; jobs compile in /dev/shm. The build FAILS if the reflection
# probe does not compile — a broken oracle image cannot exist.
FROM ubuntu:26.04

RUN apt-get update && apt-get install -y --no-install-recommends \
        binutils python3 libc6-dev docker-cli \
    && rm -rf /var/lib/apt/lists/*

COPY toolchain /opt/gcc-16.1
COPY server.py /app/server.py

# build-time proof: version + a real reflection compile
RUN LD_LIBRARY_PATH=/opt/gcc-16.1/lib64 /opt/gcc-16.1/bin/g++ --version \
    && printf '#include <meta>\nstatic_assert(^^int == ^^int);\nconstexpr auto r = ^^int;\nusing T = [:r:];\nstatic_assert(__is_same(T, int));\nint main(){}\n' > /tmp/probe.cpp \
    && LD_LIBRARY_PATH=/opt/gcc-16.1/lib64 /opt/gcc-16.1/bin/g++ \
         -std=c++26 -freflection -fsyntax-only /tmp/probe.cpp \
    && rm /tmp/probe.cpp

ENV ORACLE_PORT=8950
EXPOSE 8950
CMD ["python3", "/app/server.py"]
