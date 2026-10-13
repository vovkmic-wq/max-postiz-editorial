param([int]$Port = 8090, [switch]$NoWorker)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
# Запускаем команду в локальном окружении проекта; ключи в вывод не передаём.
$hubArgs = @('serve', '--port', $Port)
if ($NoWorker) { $hubArgs += '--no-worker' }
& .\.venv\Scripts\hub-agent.exe @hubArgs
