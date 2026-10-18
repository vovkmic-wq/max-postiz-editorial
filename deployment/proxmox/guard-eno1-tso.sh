#!/bin/bash
# Only the verified pve / eno1 / I219-V host. No interface or driver restart.
set -euo pipefail
export LC_ALL=C

mode=check
case "$#:${1-}" in
  0:|1:--check) ;;
  1:--apply) mode=apply ;;
  1:--help) printf '%s\n' 'Usage: max-postiz-eno1-tso [--check|--apply]' 'Default --check is read-only. --apply only disables TSO on the pinned host.'; exit 0 ;;
  *) printf '%s\n' 'ARGUMENT_INVALID' >&2; exit 64 ;;
esac

fail() { printf '%s\n' "$1" >&2; exit 1; }
[[ "$(hostname -s)" == pve ]] || fail HOST_MISMATCH
[[ "$(uname -r)" == *-pve ]] || fail KERNEL_NOT_PVE
command -v pveversion >/dev/null || fail PVE_TOOLS_REQUIRED
command -v ethtool >/dev/null || fail ETHTOOL_REQUIRED
[[ "$(cat /sys/class/net/eno1/address)" == f4:b5:20:4d:ed:08 ]] || fail MAC_MISMATCH
[[ "$(readlink -f /sys/class/net/eno1/device)" == /sys/devices/pci0000:00/0000:00:1f.6 ]] || fail PCI_ADDRESS_MISMATCH
[[ "$(cat /sys/class/net/eno1/device/vendor)" == 0x8086 ]] || fail PCI_VENDOR_MISMATCH
[[ "$(cat /sys/class/net/eno1/device/device)" == 0x1a1d ]] || fail PCI_DEVICE_MISMATCH
[[ "$(readlink -f /sys/class/net/eno1/device/driver)" == /sys/bus/pci/drivers/e1000e ]] || fail DRIVER_MISMATCH
driver=$(ethtool -i eno1) || fail DRIVER_QUERY_FAILED
[[ "$driver" == *$'driver: e1000e\n'* && "$driver" == *'bus-info: 0000:00:1f.6'* ]] || fail DRIVER_IDENTITY_MISMATCH

feature() {
  local name=$1 content=$2
  awk -v field="$name:" '$1 == field {print $2; found=1; exit} END {if (!found) exit 1}' <<< "$content"
}
before=$(ethtool -k eno1) || fail FEATURE_QUERY_FAILED
tso_before=$(feature tcp-segmentation-offload "$before") || fail TSO_STATE_MISSING
[[ "$tso_before" == on || "$tso_before" == off ]] || fail TSO_STATE_INVALID
protected=(generic-segmentation-offload generic-receive-offload rx-checksumming tx-checksumming)
for name in "${protected[@]}"; do
  value=$(feature "$name" "$before") || fail PROTECTED_FEATURE_MISSING
  [[ "$value" == on || "$value" == off ]] || fail PROTECTED_FEATURE_INVALID
done
if [[ "$mode" == check ]]; then
  printf '{"mode":"check","eligible":true,"interface":"eno1","tso":"%s","changed":false}\n' "$tso_before"
  exit 0
fi
[[ "$(id -u)" == 0 ]] || fail ROOT_REQUIRED
if [[ "$tso_before" == off ]]; then
  printf '%s\n' '{"mode":"apply","interface":"eno1","tso_before":"off","tso_after":"off","changed":false}'
  exit 0
fi
# The only mutation in this helper. Do not add EEE, checksum or link operations.
ethtool -K eno1 tso off || fail TSO_CHANGE_FAILED
after=$(ethtool -k eno1) || fail VERIFY_QUERY_FAILED
[[ "$(feature tcp-segmentation-offload "$after")" == off ]] || fail TSO_VERIFY_FAILED
for name in "${protected[@]}"; do
  [[ "$(feature "$name" "$after")" == "$(feature "$name" "$before")" ]] || fail PROTECTED_FEATURE_CHANGED
done
printf '%s\n' '{"mode":"apply","interface":"eno1","tso_before":"on","tso_after":"off","changed":true}'
