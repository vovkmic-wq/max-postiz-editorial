#!/bin/bash
# Only the new, blank 160 GiB scsi1 disk of this editorial VM is formatted.
set -euo pipefail
device=${CONTAINER_DISK:-/dev/sdb}
test "$(blockdev --getsize64 "$device")" -eq 171798691840
if blkid "$device" >/dev/null 2>&1 || findmnt -S "$device" >/dev/null 2>&1; then
  echo 'Refusing to format a disk with a filesystem or mount' >&2
  exit 1
fi
systemctl stop docker.service docker.socket containerd.service
mkfs.ext4 -q -L editorial-data "$device"
uuid=$(blkid -s UUID -o value "$device")
install -d /srv/editorial-containers
printf 'UUID=%s /srv/editorial-containers ext4 defaults,noatime 0 2\n' "$uuid" >>/etc/fstab
mount /srv/editorial-containers
install -d /etc/docker /etc/systemd/system/docker.service.d
cat >/etc/docker/daemon.json <<'JSON'
{"data-root":"/srv/editorial-containers/docker","features":{"containerd-snapshotter":false},"log-driver":"local","log-opts":{"max-size":"10m","max-file":"3"}}
JSON
cat >/etc/systemd/system/docker.service.d/storage.conf <<'SERVICE'
[Unit]
RequiresMountsFor=/srv/editorial-containers
SERVICE
systemctl daemon-reload
systemctl start docker
docker info --format '{{.DockerRootDir}} {{.Driver}}'
