#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/hub-agent init
# Дополнительные возможности выбираем переменными, без привязки к пути Windows.
if [ "${INSTALL_IMAGES:-0}" = "1" ]; then
    .venv/bin/python -m pip install -r requirements-images.lock
    .venv/bin/python scripts/install_image_model.py --root .
fi
# Выполняем выбранный этап установки только при включённом флаге.
if [ "${INSTALL_BROWSER:-0}" = "1" ]; then
    .venv/bin/python -m pip install -r requirements-browser.lock
    .venv/bin/python -m playwright install --with-deps chromium
fi
# Выполняем выбранный этап установки только при включённом флаге.
if [ "${INSTALL_OPENCODE:-1}" = "1" ]; then
    npm ci --ignore-scripts --no-audit --no-fund
    npm rebuild @opencode/cli --foreground-scripts
fi
