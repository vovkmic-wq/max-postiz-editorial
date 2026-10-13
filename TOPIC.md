# Тематика редакции

<!-- TOPIC_CONFIG_BEGIN -->
```json
{
  "schema_version": "1.0",
  "revision": 4,
  "channels": [
    {
      "id": "main",
      "enabled": true,
      "name": "Нейровидео",
      "theme": "Короткие видео длительностью до 60 секунд, созданные нейросетями: подборки, собственные сценарии, приёмы и разбор результатов AI видеогенерации.",
      "audience": "Русскоязычные зрители и авторы, интересующиеся нейросетевыми видеороликами.",
      "include_topics": [
        "Короткие нейросетевые видеоролики",
        "Сценарии и промпты для AI видео",
        "Разбор генерации, анимации и монтажа коротких роликов"
      ],
      "exclude_topics": [
        "Полнометражные фильмы",
        "Новости искусственного интеллекта без связи с видеогенерацией"
      ],
      "geography": [
        "весь мир"
      ],
      "input_languages": [
        "ru",
        "en"
      ],
      "output_language": "ru",
      "tone": "Понятный русский язык, точные формулировки, спокойный тон",
      "content_pillars": [
        "Ролик дня",
        "Как это сделано",
        "Промпт и сценарий",
        "Подборка нейровидео",
        "Обзор инструмента видеогенерации"
      ],
      "freshness_hours": 72,
      "publication": {
        "mode": "review",
        "timezone": "Europe/Moscow",
        "slots": [
          "09:00",
          "14:00",
          "19:00"
        ],
        "max_posts_per_day": 3,
        "min_gap_minutes": 120,
        "max_chars": 3500
      },
      "quality": {
        "min_relevance": 0.8,
        "require_source_links": true,
        "allow_full_reposts": false,
        "review_disputed_facts": true
      },
      "budget": {
        "currency": "RUB",
        "daily_llm_limit": 0,
        "monthly_growth_limit": 0
      },
      "growth": {
        "mode": "plan_only",
        "allowed_destination_ids": [],
        "experiments_per_week": 1
      },
      "monetization": {
        "enabled": false,
        "models": [],
        "max_ad_share": 0.2,
        "require_editor_approval": true
      }
    },
    {
      "id": "test",
      "enabled": true,
      "name": "Нейровидео — тест MAX",
      "theme": "Нейросетевые короткие видео и изображения с проверенными правами, авторством и ссылкой на источник.",
      "audience": "Владелец и редактор тестового канала проекта MAX.",
      "include_topics": [
        "Короткие нейросетевые видеоролики",
        "Сценарии и промпты для AI видео",
        "Разбор генерации, анимации и монтажа коротких роликов",
        "Лицензированные изображения нейроарта с автором и ссылкой на источник"
      ],
      "exclude_topics": [
        "Полнометражные фильмы",
        "Новости искусственного интеллекта без связи с видеогенерацией"
      ],
      "geography": [
        "весь мир"
      ],
      "input_languages": [
        "ru",
        "en"
      ],
      "output_language": "ru",
      "tone": "Понятный русский язык, точные формулировки, спокойный тон",
      "content_pillars": [
        "Ролик дня",
        "Как это сделано",
        "Промпт и сценарий",
        "Подборка нейровидео",
        "Обзор инструмента видеогенерации"
      ],
      "freshness_hours": 72,
      "publication": {
        "mode": "review",
        "timezone": "Europe/Moscow",
        "slots": [
          "09:00",
          "14:00",
          "19:00"
        ],
        "max_posts_per_day": 16,
        "min_gap_minutes": 120,
        "max_chars": 3500
      },
      "quality": {
        "min_relevance": 0.8,
        "require_source_links": true,
        "allow_full_reposts": false,
        "review_disputed_facts": true
      },
      "budget": {
        "currency": "RUB",
        "daily_llm_limit": 0,
        "monthly_growth_limit": 0
      },
      "growth": {
        "mode": "plan_only",
        "allowed_destination_ids": [],
        "experiments_per_week": 1
      },
      "monetization": {
        "enabled": false,
        "models": [],
        "max_ad_share": 0.2,
        "require_editor_approval": true
      }
    },
    {
      "id": "pikabu_main",
      "enabled": true,
      "name": "Нейровидео",
      "theme": "Короткие видео длительностью до 60 секунд, созданные нейросетями: подборки, собственные сценарии, приёмы и разбор результатов AI видеогенерации.",
      "audience": "Русскоязычные зрители и авторы, интересующиеся нейросетевыми видеороликами.",
      "include_topics": [
        "Короткие нейросетевые видеоролики",
        "Сценарии и промпты для AI видео",
        "Разбор генерации, анимации и монтажа коротких роликов"
      ],
      "exclude_topics": [
        "Полнометражные фильмы",
        "Новости искусственного интеллекта без связи с видеогенерацией"
      ],
      "geography": [
        "весь мир"
      ],
      "input_languages": [
        "ru",
        "en"
      ],
      "output_language": "ru",
      "tone": "Понятный русский язык, точные формулировки, спокойный тон",
      "content_pillars": [
        "Ролик дня",
        "Как это сделано",
        "Промпт и сценарий",
        "Подборка нейровидео",
        "Обзор инструмента видеогенерации"
      ],
      "freshness_hours": 72,
      "publication": {
        "mode": "review",
        "timezone": "Europe/Moscow",
        "slots": [
          "09:00",
          "14:00",
          "19:00"
        ],
        "max_posts_per_day": 3,
        "min_gap_minutes": 120,
        "max_chars": 15000
      },
      "quality": {
        "min_relevance": 0.8,
        "require_source_links": true,
        "allow_full_reposts": false,
        "review_disputed_facts": true
      },
      "budget": {
        "currency": "RUB",
        "daily_llm_limit": 0,
        "monthly_growth_limit": 0
      },
      "growth": {
        "mode": "plan_only",
        "allowed_destination_ids": [],
        "experiments_per_week": 1
      },
      "monetization": {
        "enabled": false,
        "models": [],
        "max_ad_share": 0.2,
        "require_editor_approval": true
      }
    },
    {
      "id": "pikabu_neuro_images",
      "enabled": true,
      "name": "Нейрокартинки",
      "theme": "Оригинальные нейроиллюстрации, промпты и разбор генерации",
      "audience": "Любители фантастических миров и нейроиллюстраций",
      "include_topics": [
        "нейрокартинки",
        "нейроиллюстрации",
        "генерация изображений"
      ],
      "exclude_topics": [
        "казино",
        "политические провокации"
      ],
      "geography": [
        "весь мир"
      ],
      "input_languages": [
        "ru",
        "en"
      ],
      "output_language": "ru",
      "tone": "Понятный русский язык, точные формулировки, спокойный тон",
      "content_pillars": [
        "Фантастические миры",
        "Промпт под микроскопом",
        "Разбор ошибок"
      ],
      "freshness_hours": 72,
      "publication": {
        "mode": "review",
        "timezone": "Europe/Moscow",
        "slots": [
          "12:30",
          "19:30"
        ],
        "max_posts_per_day": 2,
        "min_gap_minutes": 1,
        "max_chars": 15000
      },
      "quality": {
        "min_relevance": 0.8,
        "require_source_links": true,
        "allow_full_reposts": false,
        "review_disputed_facts": true
      },
      "budget": {
        "currency": "RUB",
        "daily_llm_limit": 0,
        "monthly_growth_limit": 0
      },
      "growth": {
        "mode": "plan_only",
        "allowed_destination_ids": [],
        "experiments_per_week": 1
      },
      "monetization": {
        "enabled": false,
        "models": [],
        "max_ad_share": 0.2,
        "require_editor_approval": true
      }
    }
  ]
}
```
<!-- TOPIC_CONFIG_END -->
