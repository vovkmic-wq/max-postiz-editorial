#!/bin/bash
# Fresh Ubuntu 24.04 guest only. Existing inference VMs are never changed.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y ca-certificates curl gnupg python3.12-venv python3-pip \
  git jq unzip rsync postgresql-client socat xvfb x11vnc novnc websockify \
  xfce4-terminal dbus-x11 openssl
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
cat >/etc/apt/sources.list.d/docker.sources <<'REPO'
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: noble
Components: stable
Signed-By: /etc/apt/keyrings/docker.asc
REPO
apt-get update -qq
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
timedatectl set-timezone Europe/Moscow
# Verified official Node.js LTS tarball, installed for this VM only.
python3 - <<'PY'
import hashlib, json, pathlib, subprocess, urllib.request
base = 'https://nodejs.org/dist'
versions = json.load(urllib.request.urlopen(base + '/index.json'))
version = next(x['version'] for x in versions if x['version'].startswith('v22.') and x['lts'])
name = f'node-{version}-linux-x64.tar.xz'
directory = pathlib.Path('/var/cache/editorial')
directory.mkdir(exist_ok=True)
checks = urllib.request.urlopen(f'{base}/{version}/SHASUMS256.txt').read().decode()
expected = next(line.split()[0] for line in checks.splitlines() if line.split()[-1] == name)
target = directory / name
urllib.request.urlretrieve(f'{base}/{version}/{name}', target)
assert hashlib.sha256(target.read_bytes()).hexdigest() == expected
subprocess.run(['tar', '-xJf', str(target), '-C', '/usr/local', '--strip-components=1'], check=True)
print('Node.js', version, 'checksum verified')
PY
install -d -m 0750 -o editorial -g editorial /opt/max-postiz
install -d -m 0700 -o editorial -g editorial /opt/max-postiz/data
# A small swap absorbs build peaks without reserving more host RAM.
if ! test -f /swapfile; then
  fallocate -l 4G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  printf '/swapfile none swap sw 0 0\n' >> /etc/fstab
fi
sysctl -w vm.max_map_count=262144
printf 'vm.max_map_count=262144\n' >/etc/sysctl.d/80-editorial.conf
docker version --format '{{.Server.Version}}'
docker compose version
