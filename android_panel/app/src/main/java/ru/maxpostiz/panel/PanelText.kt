package ru.maxpostiz.panel

import java.util.Locale

internal fun status(value: String?): String? = when (value?.trim()?.lowercase(Locale.ROOT)) {
    null, "" -> null
    "idle" -> "Ожидает задания"
    "paused" -> "На паузе"
    "waiting_dispatch" -> "Ожидание времени публикации"
    "waiting_interval" -> "Ожидание интервала между публикациями"
    "waiting_source", "no_source" -> "Ожидание подходящего источника"
    "selecting" -> "Подбор статьи"
    "preparing" -> "Подготовка статьи"
    "scheduling" -> "Назначение времени публикации"
    "reconciling" -> "Сверка результата"
    "hour_used" -> "Слот на этот час уже занят"
    "worker_busy" -> "Обработчик занят"
    "reserve_empty" -> "Резерв пуст"
    "reserve_disabled" -> "Пополнение резерва отключено"
    "reserve_satisfied" -> "В резерве достаточно статей"
    "reserve_refilled" -> "Резерв пополнен"
    "preparation_deferred" -> "Подготовка отложена"
    "draft" -> "Черновик"
    "staging_reserve_draft" -> "Создаётся черновик"
    "review_pending", "awaiting_human_review" -> "На проверке редакции"
    "prepared" -> "Подготовлено"
    "approved" -> "Согласовано"
    "ready", "automatic_ready" -> "Готово к постановке в очередь"
    "quality_ready" -> "Проверено и готово для резерва"
    "needs_attention" -> "Требуется проверка"
    "revising" -> "На доработке"
    "rejected" -> "Проверка не пройдена"
    "assigned" -> "Слот назначен"
    "queue", "scheduled" -> "В расписании"
    "pending" -> "Ожидает выполнения"
    "sending", "dispatching", "posting" -> "Идёт отправка"
    "awaiting_postiz" -> "Ожидается подтверждение Postiz"
    "awaiting_manual" -> "Ожидается ручное действие"
    "retry_wait" -> "Ожидается следующая попытка"
    "unknown", "blocked_unknown", "uncertain" -> "Исход неизвестен"
    "published" -> "Опубликовано"
    "verified", "confirmed", "delivered", "delivery_confirmed" -> "Доставка подтверждена"
    "proof_insufficient" -> "Доказательств недостаточно"
    "independent_readback" -> "Независимая сверка"
    "api_receipt" -> "Ответ площадки"
    "fixture" -> "Данные тестового стенда"
    "unverified" -> "Без подтверждения"
    "retired_no_send" -> "Закрыто без отправки"
    "record_cancelled_slot_missed_draft_preserved_no_send" -> "Закрыть слот и сохранить черновик"
    "accepted" -> "Принята"
    "running" -> "Выполняется"
    "succeeded" -> "Выполнена"
    "blocked" -> "Заблокировано"
    "failed", "error" -> "Ошибка"
    "cancelled" -> "Отменено"
    "abandoned" -> "Снято с обработки"
    "deleted" -> "Удалено"
    "missed" -> "Слот закрыт"
    "owner" -> "Владелец"
    "operator" -> "Оператор"
    "editor" -> "Редактор"
    "viewer" -> "Наблюдатель"
    else -> "Состояние не распознано"
}

internal fun blocker(value: String, fallback: String = "Сервер сообщил об ограничении. Обновите данные."): String =
    when (value) {
        "BROWSER_WORKER_CGROUP_UNAVAILABLE" -> "Серверный браузер недоступен."
        "MOBILE_HOLD_DUE_ELAPSED" -> "Срок прошёл, блокировка сохранена."
        "MOBILE_ITEM_HELD" -> "Действует ручная блокировка."
        "ARTICLE_RECONCILIATION_REQUIRED", "MOBILE_DISPATCH_LEASE_ACTIVE", "SEND_LEASE_EXPIRED" ->
            "Нужно сверить состояние слота."
        "ARTICLE_RESERVE_PREPARATION_ACTIVE" -> "Уже идёт подготовка статьи для резерва."
        "ARTICLE_QUALITY_REJECTED" -> "Статья не прошла проверку качества."
        "ARTICLE_IMAGE_PROOF_INVALID", "ARTICLE_IMAGE_SOURCE_PROOF_CONFLICT" -> "Источник изображения требует проверки."
        "ARTICLE_IMAGE_RIGHTS_REQUIRED" -> "Нужно подтвердить право на публикацию изображения."
        "ARTICLE_IMAGE_SIZE_INVALID" -> "Изображение не подошло по размеру."
        "ARTICLE_IMAGE_SOURCE_UNAVAILABLE" -> "Изображение из источника недоступно."
        "OPENCODE_PROVIDER_UNAVAILABLE" -> "Модель для подготовки статьи недоступна."
        "OPENCODE_GENERATION_FAILED" -> "Обработка статьи моделью завершилась ошибкой."
        "TRANSLATION_SERVICE_UNAVAILABLE" -> "Сервис перевода недоступен."
        "TRANSLATION_UNSPLITTABLE_INPUT" -> "Текст не удалось подготовить для перевода."
        "BROWSER_PRECLICK_FAILED" -> "Проверка в браузере перед отправкой не прошла."
        "ARTICLE_REMOTE_CHANGED", "MOBILE_HOLD_REMOTE_CHANGED" -> "Данные записи в Postiz изменились."
        "POSTIZ_UNKNOWN" -> "Postiz не подтвердил изменение. Требуется сверка."
        "POSTIZ_UNAVAILABLE", "POSTIZ_NOT_CONFIGURED" -> "Postiz недоступен."
        "PREAPPROVAL_POSTIZ_TIMEOUT_UNPROVEN", "PREAPPROVAL_POSTIZ_JOURNAL_INVALID", "PREAPPROVAL_RETIREMENT_NOT_SAFE" ->
            "Недостаточно доказательств для закрытия. Слот остаётся заблокированным."
        "OWNER_POSTIZ_ERROR_SLOT_RETIRED" -> "Просроченный слот закрыт, черновик сохранён."
        "BRIDGE_EDIT_IN_PROGRESS" -> "Серверная операция ещё выполняется."
        "MOBILE_POSTIZ_LINK_UNVERIFIED", "MOBILE_DISPATCH_LINK_AMBIGUOUS" ->
            "Связь с записью Postiz требует проверки."
        "MOBILE_POSTIZ_CALENDAR_AMBIGUOUS", "MOBILE_POSTIZ_CALENDAR_INVALID" ->
            "Расписание в Postiz требует проверки."
        "MOBILE_HOLD_NOT_SAFE", "MOBILE_HOLD_STATE_CONFLICT" ->
            "Блокировку нельзя изменить в текущем состоянии."
        "MOBILE_HOLD_OBSERVATION_STALE", "MOBILE_HOLD_REVISION_CONFLICT", "MOBILE_PLAN_CHANGED",
        "MOBILE_PLAN_CHANGED_OR_EXPIRED", "APPROVAL_STALE" -> "Данные изменились. Обновите карточку."
        "MOBILE_HOLD_PROVIDER_UNVERIFIED" -> "Postiz не подтвердил состояние записи."
        "MOBILE_HOLD_ROUTE_UNAVAILABLE", "MOBILE_ACTION_UNSUPPORTED", "TARGET_ROUTE_UNSUPPORTED" ->
            "Команда недоступна для этой площадки."
        "MOBILE_HOLD_REASON_REQUIRED" -> "Укажите причину блокировки."
        "MOBILE_CONFIRMATION_REQUIRED" -> "Подтвердите действие."
        "MOBILE_REAUTH_REQUIRED" -> "Для команды нужен свежий вход владельца."
        "MOBILE_OPERATION_UNKNOWN", "MOBILE_OPERATION_UNCERTAIN" ->
            "Исход команды неизвестен. Проверьте квитанцию."
        "MOBILE_OPERATION_NOT_ACCEPTED" -> "Шлюз не принял команду."
        "MOBILE_TARGETS_MUST_BE_PAUSED" -> "Сначала поставьте адресатов на паузу."
        "TARGET_DISABLED" -> "Адресат отключён."
        "EDITORIAL_PAUSED" -> "Проект на паузе."
        else -> fallback
    }

internal fun environmentName(value: String, fixture: Boolean): String = when {
    fixture -> "Тестовый стенд"
    value == "vm112-live" -> "Рабочий сервер"
    value.endsWith("-readonly") -> "Копия базы · только просмотр"
    else -> "Подключённый сервер"
}

internal fun targetName(id: String?, platform: String? = null): String = when (id) {
    null -> "Адресат не указан"
    "max_test" -> "MAX · тестовый канал"
    "telegram" -> "Telegram"
    "vk" -> "ВКонтакте"
    "pikabu_main" -> "Пикабу · основной профиль"
    "pikabu_neuro_images" -> "Пикабу · нейроизображения"
    else -> when (platform?.lowercase(Locale.ROOT)) {
        "max" -> "MAX"
        "telegram" -> "Telegram"
        "vk", "vkontakte" -> "ВКонтакте"
        "pikabu" -> "Пикабу"
        else -> "Адресат"
    }
}
