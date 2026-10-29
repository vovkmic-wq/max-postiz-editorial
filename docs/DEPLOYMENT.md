# Развёртывание и перенос

## Действующий порядок 0.11.5

Единственная панель — Postiz `/editorial`, один Linux server owner в
`/opt/max-postiz`, сервис `max-postiz-editorial.service`. Установлены Python
0.11.5 и схема 0008; Postiz image имеет отдельную версию
`max-postiz-linux:0.11.2-illustrated-cas`.

```sh
cd /opt/max-postiz
systemctl status max-postiz-editorial.service --no-pager
curl --fail --silent http://127.0.0.1:8092/health
.venv/bin/python deployment/linux/check-stack.py
```

Эти команды диагностические, не публикуют и не вызывают модели. Для обновления
сначала сверяют QUEUE, sending/unknown, claims и leases, сохраняют закрытые
backup обеих БД/runtime. Пауза агента не останавливает уже назначенный Postiz
QUEUE. API оставляют онлайн во время долгих QA/backup; короткий перезапуск
требует проверки времени до due и последующего health.

Не запускайте старые Windows/Hub workers параллельно. `git clone` содержит
только исходники и безопасные примеры: он не восстанавливает секреты, обе
БД, custom image, медиа, модели и авторизованный браузер. Откат с активным
или неизвестным внешним исходом требует сверки; удаление истории не является откатом.

Действующая подробная процедура: [VM_EDITORIAL](VM_EDITORIAL.md).
Результаты и открытые ограничения: [выпуск 0.11.5](RELEASE_0.11.5.md).

## Исторические процедуры и датированные проверки

## Windows / Linux
Python 3.12 и Node/npm для OpenCode. Windows: scripts/install.ps1 -Browser -SkipOpenCode (если бинарник уже установлен), Linux: sh scripts/install.sh. Опция Images устанавливает прежний необязательный image backend; для текста/загрузки PNG он не нужен.

init по умолчанию требует PostgreSQL. Задайте DATABASE_URL локально в .env.local. Никакого тихого fallback на SQLite при отказе PostgreSQL нет. Для отдельной тестовой установки используйте явный hub-agent init --sqlite.

OpenCode закреплён в package-lock.json на 2.0.17; npm ci и rebuild устанавливают бинарник. serve_opencode.py принимает его полный путь и порт, хранит доступ локально. Конфигурацию providers задайте по прежнему MAX, инструменты выключены. На новой машине cookies требуют новой авторизации.

## Локальный PostgreSQL этой установки
data/tools/pgsql содержит PostgreSQL 17.6 Windows x64 из официального EDB binary archive. SHA-256 скачанного zip: d378882abd001a186735acd6f6ba716bca6ccd192e800412d4fd15ed25376b3e. Файлы runtime, cluster и пароль исключены из исходников. scripts/local_postgres.py start/stop/status управляет этим экземпляром на 127.0.0.1:55438 без установки системной службы.

Чистый zip проекта не содержит PostgreSQL бинарник. Установите PostgreSQL штатно либо предоставьте аналогичный tools/pgsql; задайте отдельную TEST_DATABASE_URL для тестов. В тестах создаются только случайные schemas в тестовой БД.

## Postiz
Подготовлен canonical compose с [официальной установки](https://docs.postiz.com/self-host/installation/docker-compose). scripts/prepare_postiz.py получает commit GitHub, compose и dynamicconfig; фиксирует исходные SHA-256, закрывает порты на loopback, удаляет Spotlight и требует собственные секреты/явный образ Postiz. Не запускает контейнеры.

Для повторения:
```text
pip install -r requirements-deployment.lock
python scripts/prepare_postiz.py --ref dd4969e5e694cd009619a0d53cff14c21104580b
docker compose --env-file deployment/postiz/.env -f deployment/postiz/compose.yaml config --quiet
docker compose --env-file deployment/postiz/.env -f deployment/postiz/compose.yaml up -d
```
Скопируйте .env.example в ignored .env, заполните новый JWT и пароли, POSTIZ_IMAGE
с release tag/digest. Пароль БД должен быть URL-safe, иначе задайте корректно
percent-encoded DATABASE_URL. В этой установке выбран Postiz v2.25.1;
POSTIZ_DISABLE_REGISTRATION=true. Эта версия позволяет создать первого владельца
при пустой БД, затем закрывает регистрацию.

Compose сохраняет upstream Postiz PostgreSQL/Redis/Temporal и dynamicconfig;
это отдельная инфраструктура от БД редакции. Docker Desktop 4.94.0 установлен
для пользователя без изменения прежнего MAX. Стек запускается через
`scripts/postiz.ps1 start`; применяются digest из `images.lock.yaml`.
Локальный Postiz: http://localhost:4007; Temporal UI: http://localhost:8088.
Healthcheck Temporal проверяет реальный gRPC по внутреннему IP контейнера.

`scripts/bootstrap_postiz.py` создаёт или повторно подключает локального владельца
и записывает Public API в `.env.local`. После этого перезапустите панель редакции.
Пароль и ключ сохранены только в ignored `data/postiz-access.json`.
См. `deployment/postiz/README.md` и `docs/POSTIZ_DEPLOYMENT_REPORT.md`.

Postiz UI и API-key сами по себе не обеспечивают локального согласования.
Общий approval_guard_verified остаётся false. Для MAX в 0.4.0 установлен
отдельный max-browser provider: он запрашивает проверку у агента и наблюдает
pending/status; публиковать может только согласованный dispatch агента.
Установка собственного образа и перенос описаны в
[инструкции адаптера](MAX_BROWSER_ADAPTER.md).

Все восемь образов этой установки закреплены по digest. Перед сетевым
развёртыванием создайте least-privilege роли и настройте TLS/резервирование;
замените локальные URL и удалите NOT_SECURED=true.
