# Подключение общего API LLM — 12 октября 2026

Версия проекта: 0.3.2. Действующая цепочка:
редакция → OpenCode 127.0.0.1:4108 → http://10.20.30.95:8083/v1.
Основная модель — `proxmox/qwen35-9b`.

| Проверка | Результат |
|---|---|
| GET /v1/models | 200, qwen35-9b и qwen25-coder-7b |
| GET /health | 200, обе модели true |
| Qwen: прямой chat/completions | 200, ответ OK, returned_model=qwen35-9b |
| Ministral: прямой chat/completions | 200, ответ OK, returned_model=ministral3-8b-abliterated-k2200 |
| Qwen через OpenCode | probe-llm --draft: live_verified=true; получены JSON и русский пробный черновик |
| Ministral через OpenCode | probe-llm: live_verified=true, корректный JSON проверки связи |
| Выбор моделей | Основная Qwen; Ministral доступен только при явном выборе |
| Возможности | tools=false, permissions deny, local_zero_cost=true |
| Регрессия | 26 passed, 29.09 s; .test-state/llm-032-tests.xml |
| Статический анализ | Ruff src/scripts/tests прошёл |
| Панель | HTTP 200, model=proxmox/qwen35-9b, Postiz подключён, все targets paused |
| Сборка | wheel/sdist 0.3.2; исходный ZIP обновлён без приватных runtime/env |

Идентификатор `qwen25-coder-7b` сохранён общим API для совместимости.
По фактическому ответу backend это Ministral 3 8B; использовать в запросах
непосредственно `Ministral-3-8B` или внутреннее имя backend не нужно.

Первый запрос OpenCode вернул HTTP 503 из-за унаследованного HTTP-прокси.
После исключения локального адреса из прокси генерация прошла.
`scripts/serve_opencode.py` теперь добавляет loopback и явно настроенные
приватные IP моделей в NO_PROXY только для дочернего процесса, сохраняет
прежние исключения и остальные настройки прокси. Это соответствует
[поведению fetch в Bun](https://bun.sh/docs/runtime/networking/fetch).
Регрессионный тест проверяет сохранение прокси для публичных endpoint.

Активные настройки записаны в config/app.json и
data/opencode-runtime/opencode.json; переносимый пример обновлён в
config/opencode.example.json. Прежний проект MAX не изменялся.
История и промпты обновлены, версия пакета согласована с __version__.

Связь и генерация подтверждены, готовность текста к публикации — отдельно.
В пробном черновике Qwen вернул source_ids=[fixture], хотя идентификатор
материала — probe. Полный редакционный валидатор требует material.id;
probe-llm --draft проверяет наличие JSON/title/body, а не все условия
редакционной приёмки. Этот результат не был согласован или опубликован.
MAX/Пикабу остаются на паузе, облачного fallback нет.
