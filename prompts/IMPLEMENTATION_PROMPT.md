# Промпт реализации
Ты инженер редакционной системы. Реализуй самостоятельный проект на основе C:/script/20261005-Max и ручного контракта Пикабу из C:/script/20261007-pikabu. Сначала изучи фактический код, конфигурацию, миграции и тесты; сохраняй provenance исходных файлов. Не изменяй исходные проекты и не включай токены/cookies в исходники, логи или архив.

Для установки Docker и Postiz используй `prompts/POSTIZ_DEPLOYMENT_PROMPT.md`.
Уточнение 13 октября 2026: MAX browser provider реализуй по
`prompts/MAX_BROWSER_ADAPTER_PROMPT.md`; Postiz — редактор и календарь,
согласование — наш агент, отправка — прежний браузер MAX без бота.
При каждом законченном изменении обновляй `CHANGELOG.md` согласно `AGENTS.md`.

Построй цепочку RSS/HTML/JSON API и сохраняемых расписаний → PostgreSQL raw-архив/Material/Story/дедупликация → прежняя локальная LLM через OpenCode 2.0.17 → редактор, отдельные платформенные варианты, согласование и календарь → доставка через Postiz Public API либо прежний MAX transport; Пикабу — ручной пакет и подтверждение permalink.

По актуальному указанию владельца подключи сначала только прежние MAX и Пикабу, Telegram/VK оставь disabled. Возьми реальные адресаты из БД источников, не устаревших заметок. Модель и endpoint возьми из фактической конфигурации MAX; не подставляй облачного провайдера.

Актуальное уточнение подключения от 12 октября 2026: для этого проекта используй
общий API `http://10.20.30.95:8083/v1`, Qwen — `qwen35-9b`, Ministral —
совместимый слот `qwen25-coder-7b`. Основной выбор OpenCode — `proxmox/qwen35-9b`.
Не восстанавливай прежний endpoint 8080/qwen35-4b при переносе из MAX.
Локальный адрес модели должен исключаться из унаследованного HTTP-прокси
в окружении дочернего OpenCode; сохраняй остальные настройки прокси.

Обязательные свойства:
- PostgreSQL Alembic без тихого SQLite fallback; отдельная тестовая БД.
- Raw-архив с SHA-256, индекс русского поиска, версии материалов, точная/семантическая группировка.
- Разрешение хранения до загрузки; bounded pagination/checkpoint, 401/403/429/CAPTCHA остановка без обхода.
- Нулевая стоимость локальной модели, структурированный JSON, source evidence, модель без publishing/tools permissions.
- Variant и история на target; approval замораживает полный snapshot + дату + binding. Любая правка требует нового согласования.
- Durable dispatch с PostgreSQL locking/leases, независимые результаты, unknown после потери ответа, без слепых повторов.
- MAX прежний авторизованный browser под эксклюзивной блокировкой либо Bot API с правами. Пикабу экспорт с тегами и вложениями; owner_attested не выдавать за live_verified.
- Postiz draft/sync/schedule/delete/upload по официальному API. Не заявлять обязательное согласование внутри обычного Postiz UI; пока ACL/guard не доказан, schedule закрыт.
- Аутентифицированная панель, owner/editor/viewer, CSRF, вложения по hash/content type.
- Переносимый zip без runtime секретов; encrypted backup/empty-target restore и пауза после восстановления.
- Документы README, brief, ТЗ, архитектура, data model, contracts, reuse map, integration matrix, workflow, deployment, operations, acceptance, decisions, validation report.

Выполни код и проверки автономно. Прогони meaningful regression, реальные PostgreSQL locking/migrations/backup, браузерную QA панели и relocation smoke. Реальные внешние посты отправляй только после согласования конкретного текста и адресата. Недоступные LLM/Postiz/площадки укажи как оставшиеся gates с фактическими ошибками; тестовый transport не замещает live приёмку.

