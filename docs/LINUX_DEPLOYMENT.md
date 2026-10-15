# Эксплуатация редакции в Linux VM

14 октября 2026, Europe/Moscow. Проект 0.5.0 перенесён в VM 112 на
Proxmox. Существующая LLM продолжает работать на прежнем сервере.
Reddit OAuth и полностью автоматическая публикация пока не включены.
С 0.6.0 добавлены ручная очередь и приём IFTTT через Gmail:
[подключение и работа](REDDIT_INTAKE.md). Почтовой доставке нужны отдельные
настройки владельца; наличие кода не подтверждает живой IFTTT.
[Пошаговая ручная публикация в MAX](MANUAL_PUBLICATION.md) описывает вход,
редактуру в Postiz, отдельное согласование и проверку фактической доставки.

## Адреса панелей

| Компонент | Адрес | Состояние |
|---|---|---|
| Postiz | http://10.20.30.45:4007 | Проверены LOCAL login, календарь, reload и отсутствие ошибок JS |
| Панель агента | http://10.20.30.45:8090/hub | HTTP 200, восстановленная PostgreSQL |
| Браузер MAX | http://10.20.30.45:8090/api/owner-browser/vnc.html?autoconnect=1&resize=scale&path=api/owner-browser/websockify | Нужна owner-сессия панели и ручной вход в MAX |
| OpenCode | 127.0.0.1:4108 внутри VM | Обе модели дали реальный ответ |
| API LLM | http://10.20.30.95:8083/v1 | Прежняя конфигурация сохранена |
| LanguageTool | 127.0.0.1:8010 внутри VM | Проверен русский текст |
| Hub PostgreSQL | 127.0.0.1:55438 внутри VM | Перенесены 21 story/draft, 2 variants и 1 dispatch |

Firewall разрешает панели и SSH из LAN 10.20.30.0/24 и с существующего
WireGuard-клиента владельца 10.77.0.2. Порты маршрутизатора в интернет не
открывались. Дополнительный Tailscale не используется: daemon выключен,
его разрешающие правила удалены. Панели используют HTTP в частной сети;
удалённый участок защищён существующим VPN. HTTPS отдельно не настроен.

Пароли и пути к private SSH key сохранены только в игнорируемом
`data/LINUX_ACCESS.local.txt` на рабочем компьютере. В Postiz нужно выбирать
LOCAL login. Пароль аккаунта Reddit не является OAuth credential.

Адрес 10.20.30.45 получен по DHCP. Его следует закрепить на DHCP-сервере
за MAC VM. При смене IP потребуются обновление origin/proxy/firewall
и пересборка frontend Postiz.

## Виртуальная машина и службы

Ubuntu 24.04, 4 vCPU, 10 GiB RAM, swap 4 GiB. Минимум balloon поднят до
10 GiB для сборки. Системный диск 120 GiB остаётся на local-zfs.
Контейнеры размещены на отдельном диске 160 GiB пула box,
`/srv/editorial-containers/docker`, storage driver overlay2.
VM отмечена onboot; Hub, OpenCode и LanguageTool включены в systemd,
контейнеры используют restart policy. Полный reboot после переноса не проверялся.

```sh
cd /opt/max-postiz
sudo bash deployment/linux/postiz.sh ps
sudo bash deployment/linux/postiz.sh config --quiet
sudo bash deployment/linux/postiz.sh up -d --wait --wait-timeout 600
systemctl status max-postiz-hub max-postiz-opencode max-postiz-spelling
.venv/bin/python deployment/linux/verify-runtime.py
.venv/bin/python deployment/linux/qa-postiz.py
```

Не выводить `docker compose config` без `--quiet`: он раскрывает подставленные
секреты. Runtime и отчёты с приватными данными хранятся в `data/` и Docker volumes.

Postiz собран из сохранённого проверенного MAX image 0.4.0 и пересобранного
Next.js frontend для LAN origin. Сборка использует webpack, два workers,
локальные шрифты и serverExternalPackages для jsdom/isomorphic-dompurify.
Для воспроизведения нужен сохранённый seed image; полностью чистая сборка
исходного upstream Dockerfile этим переносом не подтверждается.

## Вход владельца в MAX

Используется штатный Google Chrome с существующим AppArmor-профилем Ubuntu.
Sandbox сохранён, глобальные ограничения user namespaces не отключались.
Postiz UI проверен также при NoNewPrivileges=yes, как у сервиса Hub.

```sh
sudo bash /opt/max-postiz/deployment/linux/install-owner-display.sh
sudo systemctl start max-postiz-login
```

Владелец входит в Hub и открывает страницу браузера из таблицы выше.
QR-код, телефон и OTP обрабатывает сам владелец. noVNC/VNC слушают loopback;
внешний proxy проверяет owner-роль, сеть и Origin. Viewer получает 403.
Сессия ручного входа ограничена четырьмя часами. После доступа к точному каналу
браузер закрывается, сохраняется приватный `data/max-linux-login.json`.
Подтверждение доступа не является подтверждением доставки.
GUI не включается автоматически при загрузке VM.

## Данные и откат

До изменений исходники сохранены в приватный GitHub repository
`vovkmic-wq/max-postiz-editorial`, tag `v0.4.1-pre-linux-vm-20261014`.
Перенос начат при paused targets, без active dispatches, sending/unknown
и mutation leases. Дампы восстановлены только в пустые БД.

Postiz сохранил одну PUBLISHED-запись и один DRAFT. Temporal запущен с новой
служебной историей; старые workflow histories остаются в Windows volumes.
Cookies Windows не копировались. Старый Windows worker и compose остановлены
без удаления данных. Для отката сначала остановить Linux Hub/Postiz,
сверить отправки и изменения после переноса, затем запускать Windows snapshot.
Одновременно активировать оба экземпляра нельзя.

При начальной установке sync временно отключался только для нового root zvol,
с таймерами возврата. Перед восстановлением БД и после установки подтверждён
sync=standard. Глобальные свойства пулов и другие гости не менялись.
Live move системного диска завершился QMP timeout; диск остался на local-zfs.

## Доступ Reddit и автоматический цикл

Владелец указал личное использование без монетизации. Персонализированный
текст заявки находится в игнорируемом `data/reddit-access-request.local.txt`.
[Официальная форма доступа](https://support.reddithelp.com/hc/en-us/requests/new?tf_42139884615700=api_request_type_developer_clone&ticket_form_id=14868593862164)
вернула 403 при обращении с VM. Заявка не отправлена, OAuth-приложение
и refresh token не получены. Обход блокировки и сбор через анонимный JSON
не выполнялись.

Реализованы OAuth read client, pagination/ranking ограниченной выборки,
текстовый shortlist, перевод Qwen, независимый review Ministral и обязательный
LanguageTool. `scripts/reddit_oauth.py status` показывает наличие параметров
без значений. Команда `authorize` разрешается после одобрения доступа.

Синтетическая история прошла реальную цепочку Qwen → Ministral → LanguageTool
через OpenCode; запросов Reddit и публикаций в этом тесте не было. Также
проверено восстановление уже созданного клиента после смены пароля OpenCode
при перезапуске managed server.

До production остаются разрешения на сбор, перевод и распространение,
устойчивый batch scheduler и дедупликация Reddit, автоматические согласования,
обработка удаления источников и публикаций. Автоматическое удаление текущим
MAX adapter не подтверждено. Режим десяти отдельных постов каждые четыре
часа остаётся выключенным; все адресаты на паузе. Требования сохранены в
[плане автоматизации](SERVER_REDDIT_AUTOMATION_PLAN.md).

## Документация компонентов

- [Docker Engine для Ubuntu](https://docs.docker.com/engine/install/ubuntu/)
- [Next.js serverExternalPackages](https://nextjs.org/docs/app/api-reference/config/next-config-js/serverExternalPackages)
- [Chromium и AppArmor](https://chromium.googlesource.com/chromium/src/+/main/docs/security/apparmor-userns-restrictions.md)
- [HTTP server LanguageTool](https://dev.languagetool.org/http-server)
