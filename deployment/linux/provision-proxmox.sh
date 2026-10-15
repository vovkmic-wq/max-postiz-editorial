#!/bin/bash
# Run on Proxmox as root; creates only a previously unused VM ID.
set -euo pipefail
VMID=${VMID:-112}
STORAGE=${STORAGE:-local-zfs}
SSH_PUBLIC_KEY=${SSH_PUBLIC_KEY:?Path to an owner-controlled public key}
test -s "$SSH_PUBLIC_KEY"
if qm status "$VMID" >/dev/null 2>&1 || pct status "$VMID" >/dev/null 2>&1; then
  echo "Refusing to replace existing guest $VMID" >&2
  exit 1
fi
install -d -m 700 /var/lib/vz/template/editorial
cd /var/lib/vz/template/editorial
BASE=https://cloud-images.ubuntu.com/releases/noble/release
IMAGE=ubuntu-24.04-server-cloudimg-amd64.img
curl --fail --location --proto '=https' "$BASE/SHA256SUMS" -o SHA256SUMS
grep -E "[ *]${IMAGE}$" SHA256SUMS > image.sha256
test "$(wc -l < image.sha256)" -eq 1
curl --fail --location --proto '=https' "$BASE/$IMAGE" -o "$IMAGE"
sha256sum --check image.sha256
install -d -m 700 /var/lib/vz/snippets
cat > "/var/lib/vz/snippets/editorial-$VMID.yaml" <<'CLOUD'
#cloud-config
package_update: true
packages:
  - qemu-guest-agent
runcmd:
  - [systemctl, start, qemu-guest-agent]
CLOUD
qm create "$VMID" --name max-postiz-editorial --ostype l26 --machine q35 \
  --cpu host --cores 4 --memory 10240 --balloon 10240 --agent enabled=1 \
  --net0 virtio,bridge=vmbr0 --scsihw virtio-scsi-single \
  --serial0 socket --vga serial0 --onboot 1 --startup order=20,up=30
qm importdisk "$VMID" "$PWD/$IMAGE" "$STORAGE" --format raw
qm set "$VMID" --scsi0 "$STORAGE:vm-$VMID-disk-0,discard=on,iothread=1,ssd=1"
qm resize "$VMID" scsi0 120G
qm set "$VMID" --scsi1 box:160,discard=on,iothread=1
qm set "$VMID" --ide2 "$STORAGE:cloudinit" --boot order=scsi0 \
  --ciuser editorial --sshkeys "$SSH_PUBLIC_KEY" --ipconfig0 ip=dhcp \
  --cicustom "vendor=local:snippets/editorial-$VMID.yaml"
qm start "$VMID"
qm config "$VMID"
