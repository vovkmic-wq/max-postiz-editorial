#!/bin/sh
# Ключ на узле моделей разрешает только фиксированную stdio-команду.
exec /usr/bin/ssh -T -i /opt/max-postiz/data/madlad-gpu-ed25519 \
  -o BatchMode=yes -o IdentitiesOnly=yes \
  -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
  -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile=/opt/max-postiz/data/madlad-gpu-known-hosts \
  root@10.20.30.143
