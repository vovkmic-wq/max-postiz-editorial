# Навигатор документации 0.11.5

Сверено 29 октября 2026, Europe/Moscow. Текущий статус и доказательства:
[выпуск 0.11.5](RELEASE_0.11.5.md). Основная процедура работающей VM:
[VM_EDITORIAL](VM_EDITORIAL.md). Старые результаты сохраняются по датам;
историческая команда не является поручением применить её к production.

## Основные документы

| Документ | Область |
| --- | --- |
| [README проекта](../README.md) | Краткое состояние, запуск и ограничения |
| [AGENTS](../AGENTS.md) | Правила и история полномочий владельца |
| [CHANGELOG](../CHANGELOG.md) | Append-only история версий и проверок |
| [TOPIC](../TOPIC.md) | Машиночитаемый базовый профиль; runtime-настройки сервера хранятся отдельно |

## Все руководства и отчёты

| Документ | Статус |
| --- | --- |
| [ACCEPTANCE.md](ACCEPTANCE.md) | Действующее руководство / контракт 0.11.5 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Действующее руководство / контракт 0.11.5 |
| [ARGOS_CASCADE_EVALUATION.md](ARGOS_CASCADE_EVALUATION.md) | Датированный отчёт / исследование / provenance |
| [COMPONENTS_AND_CONNECTIONS.md](COMPONENTS_AND_CONNECTIONS.md) | Датированный отчёт / исследование / provenance |
| [CONTRACTS.md](CONTRACTS.md) | Действующее руководство / контракт 0.11.5 |
| [DATA_MODEL.md](DATA_MODEL.md) | Действующее руководство / контракт 0.11.5 |
| [DECISIONS.md](DECISIONS.md) | Актуальная сводка и сохранённые исторические разделы |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Актуальная сводка и сохранённые исторические разделы |
| [E1000E_RECOVERY.md](E1000E_RECOVERY.md) | Датированный отчёт / исследование / provenance |
| [EDITORIAL_WORKFLOW.md](EDITORIAL_WORKFLOW.md) | Действующее руководство / контракт 0.11.5 |
| [HOURLY_ARTICLE_TESTS.md](HOURLY_ARTICLE_TESTS.md) | Датированный отчёт / исследование / provenance |
| [HOURLY_RESERVE_PLAN.md](HOURLY_RESERVE_PLAN.md) | Действующее руководство / контракт 0.11.5 |
| [INTEGRATIONS.md](INTEGRATIONS.md) | Действующее руководство / контракт 0.11.5 |
| [LINUX_DEPLOYMENT.md](LINUX_DEPLOYMENT.md) | Актуальная сводка и сохранённые исторические разделы |
| [LLM_CONNECTION_REPORT.md](LLM_CONNECTION_REPORT.md) | Датированный отчёт / исследование / provenance |
| [MADLAD_AUTOMATIC_WORKFLOW.md](MADLAD_AUTOMATIC_WORKFLOW.md) | Действующее руководство / контракт 0.11.5 |
| [MANUAL_PUBLICATION.md](MANUAL_PUBLICATION.md) | Отдельный ручной маршрут; не инструкция автоматического max_test |
| [MAX_BROWSER_ADAPTER.md](MAX_BROWSER_ADAPTER.md) | Актуальная сводка и сохранённые исторические разделы |
| [MAX_BROWSER_ADAPTER_REPORT.md](MAX_BROWSER_ADAPTER_REPORT.md) | Датированный отчёт / исследование / provenance |
| [MAX_POSTIZ_CONNECTION.md](MAX_POSTIZ_CONNECTION.md) | Датированный отчёт / исследование / provenance |
| [MAX_TEST_PUBLICATION_REPORT.md](MAX_TEST_PUBLICATION_REPORT.md) | Датированный отчёт / исследование / provenance |
| [NETWORK_RECOVERY.md](NETWORK_RECOVERY.md) | Датированный отчёт / исследование / provenance |
| [OPERATIONS.md](OPERATIONS.md) | Актуальная сводка и сохранённые исторические разделы |
| [POSTIZ_DEPLOYMENT_REPORT.md](POSTIZ_DEPLOYMENT_REPORT.md) | Датированный отчёт / исследование / provenance |
| [PROJECT_BRIEF.md](PROJECT_BRIEF.md) | Действующее руководство / контракт 0.11.5 |
| [REDDIT_ACCESS_REQUEST.md](REDDIT_ACCESS_REQUEST.md) | Отдельная спецификация / staging; автоматический Reddit не включён |
| [REDDIT_INTAKE.md](REDDIT_INTAKE.md) | Отдельная спецификация / staging; автоматический Reddit не включён |
| [RELEASE_0.11.5.md](RELEASE_0.11.5.md) | Действующее руководство / контракт 0.11.5 |
| [REUSE_MAP.md](REUSE_MAP.md) | Датированный отчёт / исследование / provenance |
| [SCIENTIFIC_SPELLING.md](SCIENTIFIC_SPELLING.md) | Действующее руководство / контракт 0.11.5 |
| [SERVER_REDDIT_AUTOMATION_PLAN.md](SERVER_REDDIT_AUTOMATION_PLAN.md) | Отдельная спецификация / staging; автоматический Reddit не включён |
| [STARTUP_AND_HEALTH.md](STARTUP_AND_HEALTH.md) | Актуальная сводка и сохранённые исторические разделы |
| [TECHNICAL_SPEC.md](TECHNICAL_SPEC.md) | Действующее руководство / контракт 0.11.5 |
| [TRANSLATION_20_ARTICLES.md](TRANSLATION_20_ARTICLES.md) | Датированный отчёт / исследование / provenance |
| [TRANSLATION_EVALUATION_RUBRIC.md](TRANSLATION_EVALUATION_RUBRIC.md) | Датированный отчёт / исследование / provenance |
| [TRANSLATION_MODEL_COMPARISON.md](TRANSLATION_MODEL_COMPARISON.md) | Датированный отчёт / исследование / provenance |
| [VALIDATION_REPORT.md](VALIDATION_REPORT.md) | Датированный отчёт / исследование / provenance |
| [VM_EDITORIAL.md](VM_EDITORIAL.md) | Действующее руководство / контракт 0.11.5 |
| [WORKSTATION_POWER.md](WORKSTATION_POWER.md) | Датированный отчёт / исследование / provenance |

## Развёртывание и машинные контракты

- [Postiz: старый стенд](../deployment/postiz/README.md).
- [Переводчики: backend и benchmark](../deployment/translation/README.md).
- `deployment/linux/` содержит сервисы и проверки работающего Linux-профиля.
- `prompts/`, `schemas/`, `migrations/` и примеры `config/` входят в исходники.
  Версионированные промпты и policy hashes нельзя обновлять редактурой документации.
- `.env`, приватные конфигурации, raw, фото, cookies, БД и модели остаются
  в игнорируемых файлах. Ссылки на закрытые доказательства не заменяются
  публикацией этих файлов в Git.
