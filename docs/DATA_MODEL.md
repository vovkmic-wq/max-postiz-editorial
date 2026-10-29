# Модель данных

## Серверные состояния 0.11.5

Текущая схема PostgreSQL — **0008**. В дополнение к прежнему ядру:

| Таблица | Назначение |
| --- | --- |
| editorial_control | Настройки, общая пауза, revision и runtime одного владельца |
| editorial_article_ledger | Полная история статьи, исходника, качества, claims и результатов |
| editorial_preparation_claims | Durable claim подбора/подготовки; unknown удерживает ресурс |
| publication_slots | Одно точное назначение, отправка, delivered_at и сверка результата |

0006 добавляет staging Reddit, 0007 — server control/ledger, 0008 — резерв
и слоты. Наличие таблиц Reddit не означает разрешённый доступ или включённый сбор.
Таблицы `hub_*` остаются основой variants/approvals/dispatches; их название
не означает работающую старую пользовательскую панель Hub.

Raw, медиа и модельные ответы находятся в приватном хранилище; в БД сохраняются
их точные связи и hashes. Долгий inference не удерживает sender transaction.
История отменённых и failed источников остаётся в корпусе дублей. Подтверждённый
MAX delivered_at записывается до GET Postiz, unknown не превращается в новый слот.
БД Postiz/Redis/Temporal переносятся отдельно от БД агента.

[Контракт резерва](HOURLY_RESERVE_PLAN.md) · [Эксплуатация](VM_EDITORIAL.md)

## Сохранённое ядро и схема до 0005

## Повторно использованные таблицы
config_versions: версии конфигурации; channels: редакционные профили; stories/materials: события, очищенные материалы, source/external_id/text_hash; drafts/draft_revisions: канонические версии; jobs/cursors: очередь и позиции сборщиков; budgets/generations: вызовы модели; audit_events: действия пользователей.
Legacy approvals/publication_intents/publication_attempts сохранены для истории, их действующий путь публикации закрыт HubService.

## Новые таблицы
| Таблица | Назначение и ограничение |
|---|---|
| hub_targets | Адресат, platform/route/destination, paused, binding_hash |
| hub_variants | Редакционный вариант, UNIQUE(draft_id,target_id), revision, metadata_json, media |
| hub_variant_revisions | Снимки версий, UNIQUE(variant_id,revision) |
| hub_approvals | Ревизия, дата, actor, snapshot_hash, revoked |
| hub_dispatches | Frozen snapshot, approval FK, состояние, lease/owner, попытки, внешний ID, permalink, verified |
| hub_dispatch_attempts | Результат каждой попытки доставки |
| hub_remote_drafts | Variant PK/FK, внешний ID, remote_hash, synced_revision, состояние |
| hub_bridge_posts | Postiz post PK, UNIQUE variant FK, integration/group/target, remote_hash/state/date, mutation lease |
| hub_source_archive | source_id + response_hash UNIQUE, blob_path, метаданные |
| hub_recurring_tasks | Сохраняемая задача дайджеста, окно, interval, slot, lease, attempts/error |

Индекс hub_material_search: GIN to_tsvector('russian', title || ' ' || text). Оригиналы находятся в файловом хранилище; SHA-256 и путь связывают их с SQL.

Миграции: 0001 — замороженное DDL ядра; 0002 — совместимость video-таблиц MAX; 0003 — hub-таблицы и поиск; 0004 — metadata варианта; 0005 — зеркало MAX в Postiz. В штатном режиме PostgreSQL обязателен; SQLite допустим только после явного init --sqlite для изолированной разработки.

Backup PostgreSQL читает согласованный снимок REPEATABLE READ. Его создание требует остановки worker и паузы адресатов. Restore не перезаписывает непустую БД.
