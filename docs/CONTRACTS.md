# Контракты конфигурации и API

## Действующие контракты 0.11.5

Пользователь работает только в Postiz `/editorial`. BFF обращается к API
агента на 8092: `GET /health`, `GET /internal/editorial/state`,
`GET /internal/editorial/settings`, `PUT /internal/editorial/settings` и
`POST /internal/editorial/control`. Внутренние запросы защищены отдельным
ключом и проверками origin/owner; ключ не публикуется и не передаётся модели.

`/api/max-bridge/*` обслуживает connect/exchange/submit/status и
mutation/start/finish. Он не даёт права произвольного approval. Точный
snapshot содержит текст, медиа, адресата, дату, policy и quality evidence.
Изменение snapshot требует нового согласования; sending/unknown и mutation
lease не разрешают повтор или редактирование.

`article-reserve-v1` задаёт отдельный бюджет подготовки. `article-illustrated-v2`
связывает фото и оформление. `hourly-practical-v2` фиксируется до моделей,
`article-model-feedback-v1` сохраняет все raw замечания при кратком входе Qwen.
Старые версии воспроизводятся по прежним hashes и не мигрируют массово.

`dedicated-translation-preflight-v1` возникает только в native пути до первого
HTTP. Generic splitter error, timeout и unknown не становятся доказательством
отсутствия сетевого запроса. [Подробности выпуска](RELEASE_0.11.5.md).

TOPIC содержит один неизменённый машиночитаемый базовый блок. Настройки
работающей серверной редакции и её control revision хранятся в PostgreSQL;
пример конфигурации не является снимком действующего сервера.

## Сохранённый контракт Hub 0.4.0

Версия 0.4.0: postiz.browser_bridge_enabled включает MAX bridge;
options.postiz_integration_id связывает target с настоящей Integration.
Machine API /api/max-bridge: connect/exchange/submit/status/mutation/start/finish
требует отдельного X-Max-Bridge-Key и не разрешает approval. Owner API:
/api/hub/variants/{id}/attest-original и /api/hub/max-bridge/unlock/{post_id};
editor API: /api/hub/max-bridge/sync. Подробности в MAX_BROWSER_ADAPTER.md.
TOPIC.md — единственный блок TOPIC_CONFIG_BEGIN/END с JSON schema topic.schema.json. Определяет темы, факты, источники, бюджет, лимиты, часовой пояс и редакционный режим.

config/sources.json соответствует sources.schema.json. JSON API: type=json_api, api.items_path, fields.id/text/url; title/published_at по необходимости, next_path, cursor_param, max_pages; access_ref — имя переменной среды, не её значение. Все URL страницы одного origin, опубликованные даты с timezone.

config/publishing.json соответствует publishing.schema.json. Target: id/profile_id/platform/route/destination/enabled/options. platform: max/pikabu/telegram/vk. route: max_browser/max_api/manual/postiz/disabled. options.max_chars и settings для Postiz. tasks: id/profile_id/interval_seconds/lookback_hours/enabled. Изменение binding автоматически ставит адресат на паузу.

API /api/hub:
- GET state, archive?q=..., variants/{id}/history, dispatches/{id}/attempts.
- POST drafts/{id}/variants.
- POST variants/{id}/revise: revision, body, optional title/media/metadata.
- POST variants/{id}/approve: revision, content_hash из state, due_at (Unix seconds).
- POST variants/{id}/adapt, /postiz, /sync.
- POST media: multipart file, png/jpeg/webp/mp4, максимум 100 MiB.
- POST targets/{id}/pause: строго boolean paused.
- POST dispatches/{id}/cancel, /reconcile, /attest.
- attest: revision, snapshot_hash, URL https://pikabu.ru/story/slug_ID.
- GET dispatches/{id}/export/post.json|post.txt|post.html.
- POST tick; POST sources/{id}/resume и tasks/{id}/resume (владелец).

content_hash рассчитывается fingerprint(snapshot), approval snapshot_hash — fingerprint({content:snapshot,due_at}). Snapshot включает title/body/metadata/media/sources, исходную ревизию, конфигурацию, binding, target/route/destination. Оба hash нельзя заменять хешем одного текста.

Обычные ошибки: 401 AUTH, 403 ROLE_FORBIDDEN, 409 REVISION_CONFLICT/APPROVAL_STALE/TARGET_PAUSED/REMOTE_EDIT_CONFLICT, 422 ошибка формы или политики. Секреты и подробные внешние ответы не возвращаются.

Состояния Dispatch: pending → sending → published | scheduled | awaiting_manual | failed | unknown. scheduled требует сверки; awaiting_manual → owner_attested. Неизвестный эффект не возвращается автоматически в pending. Cancel scheduled при потере ответа становится unknown.
