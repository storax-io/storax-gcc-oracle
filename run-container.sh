#!/usr/bin/env bash
# Load the prebuilt oracle image and run it (compose-free deploy target).
set -euo pipefail
cd ~/storax-g++-oracle
gunzip -c oracle-image.tgz | docker load
docker rm -f storax-gxx-oracle 2>/dev/null || true
docker run -d --name storax-gxx-oracle \
  -p 8950:8950 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  --tmpfs /dev/shm:rw,exec,size=1g \
  --memory 24g \
  --restart unless-stopped \
  storax-gxx-oracle
sleep 3
wget -qO- http://localhost:8950/health && echo
