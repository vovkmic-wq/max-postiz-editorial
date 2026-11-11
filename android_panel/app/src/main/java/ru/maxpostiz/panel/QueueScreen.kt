package ru.maxpostiz.panel

import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.time.format.DateTimeFormatter

@OptIn(ExperimentalLayoutApi::class)
@Composable
internal fun Queue(state: PanelState, model: PanelViewModel) {
    FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
        val current = state.targets.find { it.id == state.target }
        ChoiceMenu(current?.let { "${platform(it.platform)} · ${it.label}" } ?: "Все адресаты",
            listOf("Все адресаты" to null) + state.targets.map { "${platform(it.platform)} · ${it.label}" to it.id },
            enabled = !state.busy, onSelect = model::filterTarget)
        FilterChip(selected = state.reviewOnly, enabled = !state.busy,
            onClick = { model.filterReview(!state.reviewOnly) }, label = { Text("Требуют сверки") },
            modifier = Modifier.testTag("filter_review"))
    }
    if (state.section == Section.Calendar) {
        val day = state.date ?: LocalDate.now(state.zone)
        FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            TextButton(onClick = { model.calendarOffset(-1) }, enabled = !state.busy) { Text("Назад") }
            Text(day.format(dayFormat), Modifier.padding(top = 12.dp))
            TextButton(onClick = { model.calendarOffset(1) }, enabled = !state.busy) { Text("Вперёд") }
        }
        state.calendarProblem?.let { Notice("Часть данных Postiz недоступна · $it") }
    }
    if (state.items.isEmpty() && !state.busy) Text("Записей по этим условиям нет.")
    state.items.forEach { item ->
        key(item.details.string("calendar_entry_id") ?: item.id) {
            Card(onClick = { model.showItem(item.id, item.dispatchId.takeIf { state.section == Section.Calendar }) }, enabled = !state.busy,
                modifier = Modifier.fillMaxWidth().testTag("item_${item.id}")) {
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    Text(item.title, style = MaterialTheme.typography.titleMedium)
                    Text(targetLabel(item, state))
                    stamp(item.scheduledAt, state.zone)?.let { Text(it) }
                    val currentStatus = listOfNotNull(item.dispatchState, item.postizState, item.editorialState)
                        .distinct().map(::status).joinToString(" · ")
                    if (currentStatus.isNotEmpty()) Text(currentStatus)
                    if (item.needsReview) Text("Требуется сверка", color = MaterialTheme.colorScheme.error)
                    if ("MOBILE_ITEM_HELD" in item.blockers) Text("Ручная блокировка")
                    Text("Открыть карточку  ›", color = MaterialTheme.colorScheme.primary,
                        style = MaterialTheme.typography.labelLarge)
                }
            }
        }
    }
}

@Composable
internal fun Detail(item: QueueItem, state: PanelState, model: PanelViewModel) {
    Text(item.title, style = MaterialTheme.typography.headlineSmall)
    Field("Адресат", targetLabel(item, state))
    val hold = if ("MOBILE_ITEM_HELD" in item.blockers || "release_item_hold" in item.actions)
        "release_item_hold" else "hold_item"
    val commands = buildMap {
        put(hold, hold in item.actions)
        if (item.needsReview || "reconcile_dispatch" in item.actions) {
            put("reconcile_dispatch", item.dispatchId != null && "reconcile_dispatch" in item.actions)
        }
        if ("retire_preapproval_no_send" in item.actions) {
            put("retire_preapproval_no_send", item.recordId != null)
        }
    }
    CommandCard("Команды записи", commands, state, item.blockers.firstOrNull()?.let(::blocker)) { action ->
        val id = when (action) {
            "reconcile_dispatch" -> item.dispatchId
            "retire_preapproval_no_send" -> item.recordId
            else -> item.id
        }
        if (id != null) model.plan(action, id)
    }
    if (item.needsReview && item.dispatchId == null) {
        Notice("Для этой записи пока нет попытки отправки, которую можно сверить из панели.")
    }
    Field("По времени проекта", stamp(item.scheduledAt, state.zone))
    Field("UTC", item.scheduledAt)
    Field("Редакция", status(item.editorialState))
    Field("Postiz", status(item.postizState))
    Field("Отправка", status(item.dispatchState))
    Field("Слот", status(item.details.string("slot_state")))
    item.delivery?.let {
        val verified = it.optBoolean("verified", false)
        Field("Доставка", if (verified) "Подтверждена" else "Подтверждение отсутствует")
        Field("Доказательство", status(it.string("kind")))
    }
    if (item.needsReview) Notice("До завершения сверки запись удерживает слот.")
    item.blockers.forEach { Notice(blocker(it), true) }
    Text("Попытки", style = MaterialTheme.typography.titleLarge)
    val attempts = item.details.objects("attempts")
    if (attempts.isEmpty()) Text("История попыток пуста.")
    attempts.forEach {
        HorizontalDivider()
        Text(listOfNotNull(stamp(it.string("created_at"), state.zone), status(it.string("result")),
            it.string("error")).joinToString(" · "))
    }
    var technical by remember(item.id) { mutableStateOf(false) }
    TextButton(onClick = { technical = !technical }) {
        Text(if (technical) "Скрыть технические данные" else "Технические данные")
    }
    if (technical) {
        Field("Версия", item.revision)
        Field("Снимок", item.snapshotHash)
        Field("ID записи", item.id)
        Field("ID отправки", item.dispatchId)
        Field("ID Postiz", item.details.string("postiz_id"))
    }
}

private val dayFormat = DateTimeFormatter.ofPattern("dd.MM.yyyy")
private val timeFormat = DateTimeFormatter.ofPattern("dd.MM.yyyy HH:mm XXX")

internal fun stamp(value: String?, zone: ZoneId): String? = value?.let {
    runCatching { Instant.parse(it).atZone(zone).format(timeFormat) }.getOrDefault(it)
}

private fun targetLabel(item: QueueItem, state: PanelState): String =
    state.targets.find { it.id == item.targetId }?.let { "${platform(it.platform)} · ${it.label}" }
        ?: item.targetId ?: "Адресат не указан"

internal fun platform(value: String?): String = when (value?.lowercase()) {
    "max" -> "MAX"
    "telegram" -> "Telegram"
    "vk", "vkontakte" -> "ВКонтакте"
    "pikabu" -> "Пикабу"
    else -> value ?: "Площадка"
}

internal fun status(value: String?): String? = when (value) {
    "sending" -> "Идёт отправка"
    "unknown", "blocked_unknown" -> "Исход неизвестен"
    "needs_attention" -> "Требуется проверка"
    "review_pending" -> "На проверке редакции"
    "paused" -> "На паузе"
    "idle" -> "Ожидает задания"
    "scheduled", "QUEUE" -> "В расписании"
    "draft", "DRAFT" -> "Черновик"
    "published", "PUBLISHED" -> "Опубликовано"
    "verified", "confirmed", "delivered", "delivery_confirmed" -> "Доставка подтверждена"
    "proof_insufficient" -> "Доказательств недостаточно"
    "independent_readback" -> "Независимая сверка"
    "api_receipt" -> "Ответ API"
    "fixture" -> "Данные тестового стенда"
    "unverified" -> "Без подтверждения"
    "retired_no_send" -> "Закрыто без отправки"
    "record_cancelled_slot_missed_draft_preserved_no_send" -> "Закрыть слот и сохранить черновик"
    "accepted" -> "Принята"
    "running" -> "Выполняется"
    "succeeded" -> "Выполнена"
    "blocked" -> "Заблокирована"
    "failed", "ERROR" -> "Ошибка"
    "pending" -> "Ожидает выполнения"
    "cancelled" -> "Отменено"
    "missed" -> "Слот закрыт"
    "owner" -> "Владелец"
    "operator" -> "Оператор"
    "viewer" -> "Просмотр"
    else -> value
}

internal fun blocker(value: String): String = when (value) {
    "BROWSER_WORKER_CGROUP_UNAVAILABLE" -> "Контейнеру недоступна изоляция браузерного обработчика."
    "MOBILE_HOLD_DUE_ELAPSED" -> "Срок прошёл, блокировка сохранена."
    "MOBILE_ITEM_HELD" -> "Действует ручная блокировка."
    "ARTICLE_RECONCILIATION_REQUIRED" -> "Нужно сверить состояние слота."
    "ARTICLE_REMOTE_CHANGED" -> "Данные записи в Postiz изменились."
    "PREAPPROVAL_POSTIZ_TIMEOUT_UNPROVEN", "PREAPPROVAL_POSTIZ_JOURNAL_INVALID", "PREAPPROVAL_RETIREMENT_NOT_SAFE" ->
        "Недостаточно доказательств для закрытия. Слот остаётся заблокированным."
    "BRIDGE_EDIT_IN_PROGRESS" -> "Серверная операция ещё выполняется."
    "MOBILE_POSTIZ_LINK_UNVERIFIED" -> "Связь с записью Postiz требует проверки."
    else -> value
}
