$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
New-Item -ItemType Directory -Path .test-state -Force | Out-Null
# Запускаем команду в локальном окружении проекта; ключи в вывод не передаём.
& .\.venv\Scripts\ruff.exe check src tests scripts
# Проверяем условие запуска; ошибка завершает сценарий установки или проверки.
if ($LASTEXITCODE -ne 0) { throw 'Lint failed' }
# Запускаем команду в локальном окружении проекта; ключи в вывод не передаём.
& .\.venv\Scripts\python.exe -m pytest --basetemp=.test-state\pytest --junitxml=.test-state\tests.xml
# Проверяем условие запуска; ошибка завершает сценарий установки или проверки.
if ($LASTEXITCODE -ne 0) { throw 'Tests failed' }
# Запускаем команду в локальном окружении проекта; ключи в вывод не передаём.
& .\.venv\Scripts\python.exe -m pip check
# Проверяем условие запуска; ошибка завершает сценарий установки или проверки.
if ($LASTEXITCODE -ne 0) { throw 'Dependency check failed' }
