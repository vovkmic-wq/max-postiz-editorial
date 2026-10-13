# Интеграции и подтверждённые возможности

## OpenCode и LLM

OpenCode 2.0.17, provider proxmox, общий API http://10.20.30.95:8083/v1.
Основная модель qwen35-9b; qwen25-coder-7b соответствует backend
ministral3-8b-abliterated-k2200. Отдельный runtime 127.0.0.1:4108.
Tools выключены, облачного fallback нет; access.json хранится в ignored data.

Doctor проверяет версию, probe-llm --draft выполняет настоящий запрос.
В 0.3.2 подтверждены ответы обеих моделей; в 0.4.0 Qwen подготовила тестовый
черновик непосредственно для Postiz. NO_PROXY дочернего OpenCode включает
локальные адреса модели. [Отчёт LLM](LLM_CONNECTION_REPORT.md).

## MAX

Прежний адресат -79860712657053 взят из БД MAX. Browser inspect подтвердил
publish/live_verified; это доступ, а не доказательство отправки нового поста.
Прежний профиль используется под эксклюзивной блокировкой.

В 0.4.0 канал реально подключён в Postiz 2.25.1 провайдером max-browser.
Редактор и календарь Postiz передают изменения агенту; согласование и отправка
выполняются агентом через браузер. Бот не нужен. Web-сессия не заменяет Bot API
токен для других адаптеров. [Работа с MAX](MAX_BROWSER_ADAPTER.md).

Проверены подключение, draft Qwen, редактор, импорт правки и реальный
pending/status compiled provider → агент. В 0.4.1 один согласованный текст
реально опубликован, проверен после reload и новой сессии; Postiz — PUBLISHED,
Temporal — COMPLETED. Адресаты снова paused. [Отчёт](MAX_TEST_PUBLICATION_REPORT.md).

## Пикабу

Адресат user8853170, ручная публикация: экспорт, ссылка и подтверждение
владельца owner_attested с verified=false. Pikabu_main без привязки выключен.

## Postiz и Vezdepost

Public API подключён локально: http://localhost:4007/api/public/v1.
API-key в .env.local; пароль в data/postiz-access.json. Поддерживаются draft,
schedule, upload, чтение и delete. Для MAX browser bridge расширена projection
posts (image/childrenPost) и integrations (accountId/refreshNeeded).

Обычный UI Postiz способен публиковать самостоятельно, поэтому общий
approval_guard_verified=false и POSTIZ_APPROVAL_GUARD_REQUIRED сохраняются
для обычных маршрутов. MAX защищён отдельным provider и mutation guard.
Права ключа Postiz не дают согласования агенту.

Vezdepost Bot API provider изучен ранее; его provenance сохранён в
deployment/postiz-max/integration-plan.json как исторический вариант.
Он не установлен и не участвует в текущей браузерной цепочке.
Telegram/VK выключены. Нет слепого повторения POST после timeout/5xx;
unknown требует операторского расследования.

