# Модель данных
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

