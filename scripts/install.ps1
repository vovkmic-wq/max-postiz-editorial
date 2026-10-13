param([string]$Python = 'py', [switch]$Images, [switch]$Browser, [switch]$SkipOpenCode)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
# Проверяем условие запуска; ошибка завершает сценарий установки или проверки.
if (-not (Test-Path '.venv\Scripts\python.exe')) {
    # Проверяем условие запуска; ошибка завершает сценарий установки или проверки.
    if ($Python -eq 'py') { & py -3.12 -m venv .venv } else { & $Python -m venv .venv }
    # Проверяем условие запуска; ошибка завершает сценарий установки или проверки.
    if ($LASTEXITCODE -ne 0) { throw 'Python environment creation failed' }
}
# Запускаем команду в локальном окружении проекта; ключи в вывод не передаём.
& .\.venv\Scripts\python.exe -m pip install -r requirements.lock
# Проверяем условие запуска; ошибка завершает сценарий установки или проверки.
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
# Запускаем команду в локальном окружении проекта; ключи в вывод не передаём.
& .\.venv\Scripts\python.exe -m pip install --no-deps -e .
# Проверяем условие запуска; ошибка завершает сценарий установки или проверки.
if ($LASTEXITCODE -ne 0) { throw 'Application installation failed' }
# Запускаем команду в локальном окружении проекта; ключи в вывод не передаём.
& .\.venv\Scripts\hub-agent.exe init
# Проверяем условие запуска; ошибка завершает сценарий установки или проверки.
if ($LASTEXITCODE -ne 0) { throw 'Initialization failed' }
# Изображения и браузер устанавливаем по флагам; окружение всегда создаём заново.
if ($Images) {
# Запускаем команду в локальном окружении проекта; ключи в вывод не передаём.
    & .\.venv\Scripts\python.exe -m pip install -r requirements-images.lock
    # Проверяем условие запуска; ошибка завершает сценарий установки или проверки.
    if ($LASTEXITCODE -ne 0) { throw 'Image dependency installation failed' }
# Запускаем команду в локальном окружении проекта; ключи в вывод не передаём.
    & .\.venv\Scripts\python.exe scripts/install_image_model.py --root .
    # Проверяем условие запуска; ошибка завершает сценарий установки или проверки.
    if ($LASTEXITCODE -ne 0) { throw 'Local image model installation failed' }
}
# Проверяем условие запуска; ошибка завершает сценарий установки или проверки.
if ($Browser) {
# Запускаем команду в локальном окружении проекта; ключи в вывод не передаём.
    & .\.venv\Scripts\python.exe -m pip install -r requirements-browser.lock
    # Проверяем условие запуска; ошибка завершает сценарий установки или проверки.
    if ($LASTEXITCODE -ne 0) { throw 'Browser dependency installation failed' }
# Запускаем команду в локальном окружении проекта; ключи в вывод не передаём.
    & .\.venv\Scripts\python.exe -m playwright install chromium
    # Проверяем условие запуска; ошибка завершает сценарий установки или проверки.
    if ($LASTEXITCODE -ne 0) { throw 'Browser installation failed' }
}
# Проверяем условие запуска; ошибка завершает сценарий установки или проверки.
if (-not $SkipOpenCode) {
    npm.cmd ci --ignore-scripts --no-audit --no-fund
    # Проверяем условие запуска; ошибка завершает сценарий установки или проверки.
    if ($LASTEXITCODE -ne 0) { throw 'OpenCode installation failed' }
    npm.cmd rebuild @opencode/cli --foreground-scripts
    # Проверяем условие запуска; ошибка завершает сценарий установки или проверки.
    if ($LASTEXITCODE -ne 0) { throw 'OpenCode binary preparation failed' }
}
