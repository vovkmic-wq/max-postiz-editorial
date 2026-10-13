# Эксплуатация
Для MAX через редактор и календарь Postiz используйте
[инструкцию адаптера 0.4.0](MAX_BROWSER_ADAPTER.md).
Mutation lease, sending/unknown и paused проверяются отдельно; не снимаются
перезапуском или истечением времени. Ключ моста не даёт права согласования.
Все команды выполняются из корня в .venv. На Windows: .venv/Scripts/hub-agent.exe; на Linux: .venv/bin/hub-agent.

## Основные команды
```text
hub-agent doctor
hub-agent probe-llm --draft
hub-agent collect SOURCE_ID
hub-agent variants DRAFT_ID
hub-agent inspect-target max_test
hub-agent serve --port 8090
hub-agent worker
hub-agent tick
hub-agent import-history PATH_TO_SOURCE_AGENT_DB
hub-agent import-history PATH_TO_SOURCE_AGENT_DB --apply
hub-agent export-project dist/editorial-hub.zip
hub-agent backup data/backups/snapshot.enc
hub-agent restore data/backups/snapshot.enc
```
serve включает фоновый worker. --no-worker даёт только панель. Не запускайте serve-worker и отдельный worker без необходимости; PostgreSQL защищает dispatch, браузер MAX требует эксклюзивного профиля.

## Сбои
- OPENCODE_PROVIDER_UNAVAILABLE: проверить общий API 10.20.30.95:8083, GET /v1/models и NO_PROXY дочернего OpenCode; затем probe-llm. Не менять модель автоматически.
- SOURCE_HTTP_429/CAPTCHA: источник заблокирован; изучить причину и Retry-After, возобновление только владельцем через /api/hub/sources/{id}/resume.
- unknown/SEND_LEASE_EXPIRED: проверить фактическую площадку, затем reconcile; повторная отправка автоматически не выполняется.
- REVISION_CONFLICT: обновить панель, заново сохранить и согласовать.
- POSTIZ_APPROVAL_GUARD_REQUIRED: экземпляр Postiz не прошёл контроль publishing permissions.
- Ошибка задачи дайджеста после трёх попыток: устранить причину и повторить из раздела расписания /hub либо POST /api/hub/tasks/{id}/resume. Удалённые из конфигурации задачи отключаются; прежний slot сохраняется при повторе.
- BUFFER/lease/worker: завершить worker до backup, проверить отсутствие leased jobs/sending dispatches.

## Backup и перенос
Задайте HUB_BACKUP_KEY отдельно в ignored .env.local (Fernet key). Потеря ключа делает backup невосстановимым. Перед backup остановите worker, поставьте все targets и legacy channels на паузу. Архив содержит историю, конфигурацию, raw-архив, экспорт и медиа; ключи доступа и cookies исключены.

Для restore задайте новый DATABASE_URL на пустую БД, init, затем restore. Команда мигрирует схему, но не создаёт runtime-строки до восстановления. После переноса targets остаются paused, sending становится unknown. Повторно настройте LLM и авторизацию.

import-history открывает SQLite источника read-only в одной read-транзакции; повторный запуск пропускает существующие primary keys. Исторические approvals revoked, intents/drafts archived. Он не переносит активные jobs, cursors, браузер, медиа и токены. Совпадение PK разных исходных проектов требует отдельного анализа, не автоматического объединения.

## Ограничения текущего запуска
Процессы редакции локальные, их автозапуск служб не установлен. Docker Desktop
установлен для текущего пользователя и добавлен в запуск при входе Windows;
контейнеры Postiz используют restart policy. Управление Postiz:
`scripts/postiz.ps1 start|stop|status|check`. Пароль владельца хранится в
`data/postiz-access.json`; Public API подключён через `.env.local`.

Локальная роль hub_admin привилегированная; для сети создайте app/migration/backup
роли, ограничьте pg_hba, TLS и reverse proxy. Общий API LLM подключён в 0.3.2;
готовность конкретного текста определяется редакционной валидацией. При каждом изменении
обновляйте `CHANGELOG.md` по `AGENTS.md`.

Основная модель задаётся `opencode.model_id` в config/app.json:
`proxmox/qwen35-9b`. Для явного выбора второй модели используйте
`proxmox/qwen25-coder-7b` (Ministral) или OPENCODE_MODEL_ID.
Провайдеры и общий endpoint описаны в config/opencode.example.json;
активный файл — data/opencode-runtime/opencode.json.
После изменения конфигурации OpenCode перезапустите его, затем панель,
поскольку пароль локального доступа пересоздаётся при старте OpenCode.

