#!/bin/bash
# Меняем только внутренний API редакции и выводимые из эксплуатации порты Hub.
set -euo pipefail
case "${1:-}" in
  prepare)
    ufw allow from 172.16.0.0/12 to any port 8092 proto tcp
    ;;
  retire-hub)
    ufw --force delete allow from 172.16.0.0/12 to any port 8091 proto tcp
    ufw --force delete allow from 10.20.30.0/24 to any port 8090 proto tcp
    ufw --force delete allow from 10.77.0.2 to any port 8090 proto tcp
    ;;
  *)
    echo 'Использование: editorial-firewall.sh prepare|retire-hub' >&2
    exit 2
    ;;
esac
