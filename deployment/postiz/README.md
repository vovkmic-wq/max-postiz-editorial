# Локальный Postiz

Адрес редактора: http://localhost:4007. Temporal UI: http://localhost:8088.
Порты привязаны к loopback. Состав стека: Postiz, PostgreSQL, Redis,
Temporal, отдельная БД Temporal, Elasticsearch, Temporal UI и admin-tools.

```powershell
.\scripts\postiz.ps1 check
.\scripts\postiz.ps1 start
.\scripts\postiz.ps1 status
.\scripts\postiz.ps1 stop
```

Скрипт находит Docker Desktop после установки даже в терминале со старым PATH.
Имена ресурсов принадлежат Compose-проекту `max-postiz`. Если существует
`images.lock.yaml`, при запуске применяются закреплённые digest образов.
Обновляй lock только после явного скачивания и проверки новых образов:
`python scripts/lock_postiz_images.py --docker <полный-путь-к-docker.exe>`.
Остановка сохраняет volumes. Для нового запуска сначала запусти Docker Desktop.

Секреты стека находятся только в игнорируемом `deployment/postiz/.env`.
Вход локального владельца и API-ключ находятся только в игнорируемом
`data/postiz-access.json`. Не пересылай и не включай эти файлы в исходный ZIP.

Создание или повторное подключение владельца:

```powershell
.\.venv\Scripts\python.exe scripts/bootstrap_postiz.py
```

Скрипт сохраняет пароль до запроса, повторно входит в имеющуюся учётную запись
и подключает Public API через `.env.local`. После изменения `.env.local`
перезапусти панель редакции. Регистрация выключена; Postiz v2.25.1 разрешает
создать первого владельца только пока число организаций равно нулю.

`NOT_SECURED=true` необходим для локальной HTTP-авторизации. Для сетевой
установки нужны HTTPS, корректные URL и удаление этой настройки.
Проверка Docker/Postiz не включает публикацию в социальных сетях.
MAX и Пикабу продолжают использовать маршруты редакции; они не становятся
нативными каналами Postiz от одной установки сервера.

Исходный Compose и dynamicconfig закреплены по официальному репозиторию в
`upstream-manifest.json`. Выполненные проверки фиксируются в отчёте проекта.
