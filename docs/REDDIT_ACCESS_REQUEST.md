# Черновик запроса доступа к Reddit для перевода публикаций

## Проверка 19 октября 2026

Переписка существующего тикета проверена в авторизованном браузере Gmail:
сохранились общий ответ поддержки от 14 октября и наше уточнение от 17 октября.
Более нового ответа или одобрения в этой переписке нет. Поиск писем поддержки
также показал только эту тему. Подключение Gmail API требует повторной
авторизации, но браузерный просмотр позволил проверить письма без неё.

Read-only OAuth status на рабочей VM подтвердил: одобрение доступа и переводов
не зафиксировано; client ID, client secret, refresh token и user agent отсутствуют.
API-сбор и Reddit-автоматизация выключены; сетевое чтение Reddit не выполнялось.
Приватные доказательства находятся в ignored data/reddit-api-mail-check-20261019.local.*.
Следующий шаг — решение команды доступа в существующей переписке, затем разрешённая
регистрация клиента и минимальное чтение. Повторную заявку не создавали.

## Текущий статус — 17 октября 2026

14 октября отправлено обращение на официальный контакт API. Reddit Support
создала тикет и прислала общий ответ со ссылками на документацию и форму;
это не одобрение доступа. 17 октября форма Data Access Request открылась
в рабочем браузере. Доступность формы непосредственно с Linux VM повторно
не проверялась.

В существующую переписку отправлено уточнение по полям формы с просьбой
передать сценарий на рассмотрение команды доступа для приложений вне Devvit.
Запрошено отдельное решение о read-only API, локальном inference,
переводе и внешнем распространении, а также правила регистрации,
ограничения объёма, хранения и удаления. Новую заявку-дубликат не подавали.
Отправка подтверждена Gmail; получение уточнения командой рассмотрения
и одобрение ещё не подтверждены.

Точный текст, адресат, номер тикета и снимок отправки находятся только в
игнорируемых `data/reddit-access-followup-20261017.local.txt` и
`data/reddit-access-followup-20261017.local.jpg`. Персональные сведения,
пароль аккаунта, client secret и токены в документацию не включаются.

OAuth-приложение и refresh token пока не получены. Флаги одобрения не менялись,
API-сбор и автоматическое расписание не включены. Ниже сохранён общий шаблон
описания сценария для дальнейшей переписки.

## Subject

Request for approved Reddit data access and translated distribution in a MAX channel

## Project description

I am planning a self-hosted editorial service for a Russian-language MAX channel.
It would select up to ten suitable English-language Reddit posts every four hours,
with a maximum of sixty separate publications per day. The intended topics are
interesting stories, science and humor. Political and NSFW content would be excluded.

I am requesting guidance and explicit authorization for this use case, including
translation and distribution outside Reddit. I understand that standard API access
does not itself establish the right to modify or redistribute users' content.
I would use only content and rights that you and the relevant rights holders permit.

The proposed processing would use local language models for translation and quality
checking, not model training. Posts would link to their original Reddit sources
and would be identified as machine-assisted translations. The intended access is
read-only; the service would not vote, comment, message users or act as a moderation tool.

The service is not deployed for Reddit collection yet, and no approved OAuth app
has been created for this project. Please advise on the appropriate registration
and access route, and whether this external workflow can be approved under your
current rules or requires a separate agreement. The processing and destination
are outside Reddit, rather than an app running only within Reddit.

Before deployment, I would implement the required access controls, rate-limit
handling, retention limits and removal workflows. The existing MAX delivery
adapter does not yet automate deletion of published messages; that capability
or an acceptable removal process must be resolved before enabling distribution.
I would not claim that it is already implemented.

## Fields for the owner

- Reddit username: enter the actual username, not the email address.
- Project or source URL: provide a real accessible reference if requested.
- Monetization: state the actual status; do not assume that a personal account
  makes every future use non-commercial.
- Requested retention/removal handling: confirm the approved requirements
  and implementation before enabling the collector.

## Official submission route

[Reddit access request form](https://support.reddithelp.com/hc/en-us/requests/new?tf_42139884615700=api_request_type_developer_clone&ticket_form_id=14868593862164)

[Responsible Builder Policy](https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy),
[Data API Terms](https://redditinc.com/policies/data-api-terms).

## Поля действующей формы

Проверено в браузере 17 октября 2026:

1. Data Access Request → I’m a developer.
2. I’m a developer and want to build a Reddit App that does not work in the Devvit ecosystem.
3. Reddit account name; benefit/purpose for Redditors; detailed platform activity;
   missing Devvit capabilities; real source/platform URL; intended subreddits;
   optional app account name.

В уточнении описан личный проект без монетизации: read-only scope `read`,
до 500 кандидатов за четырёхчасовой цикл, до 60 отдельных внешних публикаций
в сутки. Это запрошенные верхние пределы, а не разрешение или текущий сбор.
Предложено ограничить источники согласованным списком сообществ и снизить
объём, если это требуется для одобрения. Приватный GitHub-репозиторий не
представлялся как общедоступный исходный код. Отдельно раскрыто, что
автоматическое удаление внешних MAX-копий пока не реализовано.

Responsible Builder Policy требует явного одобрения до обращения к API
и запрещает несколько запросов для одного сценария. Продолжать существующий
тикет; при требовании другой очереди попросить перевод обращения. Не создавать
другие аккаунты и не регистрировать повторную заявку ради обхода рассмотрения.

## Действия после решения Reddit

1. Сохранить решение и точный разрешённый сценарий в ignored data. Уточнить
   ограничения локального inference и внешнего распространения. Общий ответ
   со ссылками, наличие OAuth или кнопки Create app не являются одобрением.
2. Следовать указанному Reddit порядку регистрации. Текущий helper рассчитан
   на confidential OAuth client с secret, scope `read` и точным callback
   `http://localhost:8767/callback`. При другом разрешённом механизме сначала
   адаптировать код. Client ID/secret/user agent хранить в ignored `.env.local`.
3. Только при реальном разрешении API настроить флаг `reddit.access_approved`.
   Проверить `scripts/reddit_oauth.py status`, затем выполнять `authorize`
   с участием владельца. Для браузера на рабочем компьютере нужен SSH forward
   порта 8767 к VM; для браузера на VM callback остаётся локальным.
4. Отдельно провести минимальное разрешённое чтение без перевода и публикации.
   `status` сейчас всегда сообщает `live_reddit_verified=false` и не выполняет
   сетевую проверку; наличие токена не доказывает рабочее API.
5. До автопубликаций реализовать scheduler, полную дедупликацию,
   policy approvals, проверку удаления источников и удаление внешних копий.
   Право перевода/распространения и согласованный процесс удаления проверять
   отдельно от API-доступа. При отказе запросить допустимый ограниченный формат;
   не включать его автоматически вместо исходного сценария.
