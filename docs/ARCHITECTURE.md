# Архитектура
```mermaid
flowchart LR
  R[RSS / HTML / JSON API / расписание] --> C[Сборщики и checkpoint]
  C --> P[(PostgreSQL: Material / Story / Jobs)]
  C --> A[Raw-архив SHA-256]
  P --> O[OpenCode 2.0.17]
  O --> L[Локальная Qwen / Ministral]
  L --> D[Draft / Variant / источники]
  D --> Z[Postiz: редактор и календарь MAX]
  Z --> B[MAX browser bridge: зеркало и проверка]
  B --> H[Наш агент: approval версии и даты]
  H --> Q[Durable Dispatch]
  Q --> M[Прежний браузер MAX]
  Q --> S[Verified ID / ссылка]
  S --> Z
  D --> K[Пикабу: ручной пакет и подтверждение]
```

SQLAlchemy/Alembic управляют PostgreSQL архива редакции. Postiz имеет отдельные
PostgreSQL/Redis/Temporal и не заменяет эту БД. Повторно использованные модули
MAX живут в самостоятельном namespace; старый путь согласования закрыт HubService.

Для MAX provider Postiz передаёт запрос review и наблюдает pending/status.
Postiz QUEUE не создаёт approval. Зеркало фиксирует ревизию, точное время,
hash настроек, текста и медиа; отправку разрешает только агент.
Mutation guard блокирует изменение во время sending/unknown. Перед внешним
запросом агент фиксирует sending и повторно читает авторитетный Postiz.

Потеря ответа остаётся unknown до сверки; exactly-once не заявляется.
Completed возвращается Postiz только при verified доставке. Общий guard для
обычных Postiz-провайдеров остаётся неподтверждённым; TG/VK отключены.
Подробнее: [адаптер MAX](MAX_BROWSER_ADAPTER.md).

