#!/bin/bash
# Run as root on the existing Proxmox host. Does not start, stop or reboot guests.
set -euo pipefail

test "$(id -u)" -eq 0
command -v qm >/dev/null

test "$(qm config 109 | sed -n 's/^name: //p')" = llama-inference
test "$(qm config 112 | sed -n 's/^name: //p')" = max-postiz-editorial
test "$(systemctl is-enabled pve-guests.service)" = enabled

stamp=$(date +%Y%m%d-%H%M%S)
umask 077
qm config 109 > "/root/109.conf.before-editorial-autostart-$stamp"
qm config 112 > "/root/112.conf.before-editorial-autostart-$stamp"

qm set 109 --onboot 1 --startup order=10,up=90
qm set 112 --onboot 1 --startup order=20,up=30

for vm in 109 112; do
  qm config "$vm" | grep -E '^(name|onboot|startup):'
done
echo 'VM110 remains a stopped reserve. Other guests are unchanged.'
