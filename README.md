# Редакция MAX / Пикабу / Postiz
Связка Python 3.12 + PostgreSQL + OpenCode 2.0.17 для сбора материалов, архива, дедупликации, подготовки публикаций и согласования отдельных версий для каждой площадки.

Версия проекта: **0.6.0**. [История версий](CHANGELOG.md) обновляется при каждом изменении
по [правилам проекта](AGENTS.md). [Промпт установки Docker и Postiz](prompts/POSTIZ_DEPLOYMENT_PROMPT.md)
сохранён вместе с исходным промптом реализации.

Ручная очередь Reddit и вход IFTTT через Gmail доступны в `/hub`.
Настройка: [Reddit IFTTT и ручной отбор](docs/REDDIT_INTAKE.md).
Подключение внешних аккаунтов и автопубликация не подтверждаются наличием формы.

План следующего этапа от 14 октября: [VM и автоматический канал Reddit](docs/SERVER_REDDIT_AUTOMATION_PLAN.md),
[промпт](prompts/REDDIT_AUTOMATION_PROMPT.md), [пример спецификации](config/reddit_automation.example.json).
Выбран режим 10 отдельных постов каждые 4 часа; пока он не реализован и не включён.

Текущий запуск в Linux VM: [панель агента](http://10.20.30.45:8090/hub), [Postiz](http://10.20.30.45:4007). [Эксплуатация и откат](docs/LINUX_DEPLOYMENT.md). Доступ из LAN и через существующий WireGuard. Пароли — только в игнорируемом `data/LINUX_ACCESS.local.txt`; после входа в Hub используется HttpOnly-сессия.

## Подключения в этой установке
- MAX: прежний тестовый канал `-79860712657053`, профиль `max_test`. 13 октября в 15:21 по Москве опубликован один согласованный тестовый текст через браузер; сохранение после reload и новой сессии проверено. Канал возвращён на паузу.
- Пикабу: `user8853170`, профиль `pikabu_neuro_images`. Ручной экспорт и подтверждение ссылкой; автоматическая отправка и чтение владельца через API не заявляются.
- `pikabu_main` отключён: в исходном проекте нет привязанного адресата.
- Telegram и VK отключены по текущему указанию.
- LLM: общий API `http://10.20.30.95:8083/v1`; основная модель `proxmox/qwen35-9b`, второй выбор `proxmox/qwen25-coder-7b` (Ministral 3 8B). Реальные ответы обеих моделей через OpenCode подтверждены; получен пробный черновик Qwen. [Проверка подключения](docs/LLM_CONNECTION_REPORT.md).
- PostgreSQL 17 в Linux доступен на loopback 55438; база `editorial_hub` восстановлена из Windows. Исходный Windows экземпляр сохранён для тестов и отката. Базы не выставляются в LAN.
- Postiz 2.25.1 с собственным провайдером **MAX — браузер** развёрнут на http://10.20.30.45:4007. Редактор и календарь передают версии нашему агенту. Provider bridge проверен на прежнем черновике без отправки; браузер Linux требует нового входа владельца. Данные входа — только в `data/postiz-access.json`.

В Postiz подготовлен тестовый черновик от Qwen; проверен импорт сохранённой правки.
Все адресаты на паузе. В 0.4.1 выполнена одна согласованная тестовая публикация;
Postiz показал PUBLISHED. [Отчёт живого теста](docs/MAX_TEST_PUBLICATION_REPORT.md).
[Порядок работы с MAX](docs/MAX_BROWSER_ADAPTER.md),
[результаты проверки и ограничения](docs/MAX_BROWSER_ADAPTER_REPORT.md),
[промпт адаптера](prompts/MAX_BROWSER_ADAPTER_PROMPT.md).

Адресаты изначально на паузе. Снимите паузу выбранного адресата в панели, сохраните текст и согласуйте конкретную версию с датой. Пикабу выдаёт экспорт; MAX отправляет только согласованную запись. Источники скопированы отключёнными: редактор включает нужные после проверки настроек и прав.

## Запуск на этой машине
```powershell
.\.venv\Scripts\python.exe scripts/local_postgres.py start
# В отдельном терминале, если OpenCode ещё не запущен:
.\.venv\Scripts\python.exe scripts/serve_opencode.py --binary data/tools/opencode/opencode.exe --port 4108
# В другом терминале:
.\scripts\start.ps1
```
Процессы запускаются локально, службы Windows не установлены. Для остановки панели/OpenCode завершите их терминалы; PostgreSQL: `python scripts/local_postgres.py stop`.

Postiz: запустите Docker Desktop, затем `.\scripts\postiz.ps1 start`.
Проверка: `.\scripts\postiz.ps1 status`; остановка с сохранением данных:
`.\scripts\postiz.ps1 stop`. [Инструкция](deployment/postiz/README.md),
[результаты установки](docs/POSTIZ_DEPLOYMENT_REPORT.md).

## Чистая установка и документы
Смотрите [развёртывание](docs/DEPLOYMENT.md), [эксплуатацию](docs/OPERATIONS.md), [интеграции](docs/INTEGRATIONS.md), [приёмку](docs/ACCEPTANCE.md) и [отчёт проверки](docs/VALIDATION_REPORT.md).

[Исходный промпт](prompts/IMPLEMENTATION_PROMPT.md), [описание проекта](docs/PROJECT_BRIEF.md), [ТЗ](docs/TECHNICAL_SPEC.md), [архитектура](docs/ARCHITECTURE.md), [контракты](docs/CONTRACTS.md), [модель данных](docs/DATA_MODEL.md), [повторное использование MAX](docs/REUSE_MAP.md), [редакционный процесс](docs/EDITORIAL_WORKFLOW.md), [решения](docs/DECISIONS.md).

