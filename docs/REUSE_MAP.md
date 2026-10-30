# Что перенесено из MAX

**Область документа, сверено для 0.11.5 29 октября 2026:** История заимствования и исходные hashes. Они описывают provenance, а не текущие переработанные файлы.
Актуальные эксплуатация и состояние: [VM](VM_EDITORIAL.md), [выпуск 0.11.5](RELEASE_0.11.5.md).

Источник: C:/script/20261005-Max. Дополнительный контракт Пикабу: C:/script/20261007-pikabu. Новое расположение: C:/script/20261011-Max postiz, namespace max_postiz_agent. Исходники проектов не редактировались; read-only чтение конфигурации/БД и использование прежнего браузерного профиля согласованы указанием владельца.

docs/reuse-manifest.json фиксирует исходные пути, новые пути и SHA-256 85 базовых файлов на момент копирования. Это provenance, а не хеш текущих переработанных файлов.

| Наработка | Повторное использование |
|---|---|
| config.py / schemas / TOPIC.md | Проверка конфигурации и тематик; добавлена схема publishing и JSON API |
| network.py / collectors.py | SSRF-ограничения, HTML/RSS, условные запросы; добавлен JSON API и блокировка источников |
| service.py / editorial.py | Кластеризация, бюджеты, model task, факты/источники, ревизии |
| adapters.py / prompts | Версионированный OpenCode и MAX транспорт; добавлена adapt shape |
| db.py / migrations | Базовые таблицы; PostgreSQL обязателен и расширен hub-схемой |
| panel_auth.py / api.py | Роли, сессии, audit; /hub добавляет общий процесс |
| browser*.py | Прежний MAX браузер и эксклюзивная блокировка профиля |
| backup.py / portability.py | Зашифрованное состояние и allowlist zip, добавлены новые таблицы/архив |
| tests | Базовые регрессионные проверки плюс новые PostgreSQL/dispatch cases |
| Pikabu pikabu.py | Проверка permalink, тегов и ручного подтверждения адаптирована в pikabu_metadata.py |

Раздельные runtime config, .venv, PostgreSQL, OpenCode и журналы. Секреты лежат в ignored .env.local/data; cookies остаются в исходном профиле. Исходный старый MAX outbox не активируется в новой связке.
