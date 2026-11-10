#!/bin/sh
set -eu
umask 077
test -s /run/monitor/vnc-password
export DISPLAY=:97
Xvfb "$DISPLAY" -screen 0 1280x900x24 -nolisten tcp &
display_pid=$!
trap 'kill "$display_pid" "${vnc_pid:-}" "${web_pid:-}" 2>/dev/null || true' EXIT
trap 'exit 130' INT TERM
n=0
until xdpyinfo >/dev/null 2>&1; do
    n=$((n + 1))
    [ "$n" -lt 50 ] || exit 1
    sleep .1
done
x11vnc -display "$DISPLAY" -localhost -rfbport 5900 -rfbauth /run/monitor/vnc-password -forever -shared -quiet &
vnc_pid=$!
websockify --web=/usr/share/novnc 6080 127.0.0.1:5900 &
web_pid=$!
python -B /opt/monitor/runner.py login
