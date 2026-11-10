package ru.maxpostiz.panel

import android.content.Intent
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp

@OptIn(ExperimentalMaterial3Api::class)
@Composable
internal fun PanelScreen(state: PanelState, model: PanelViewModel, openBrowser: (Intent) -> Unit,
                         copyPassword: (String) -> Unit, lock: () -> Unit) {
    val caps = requireNotNull(state.capabilities)
    BackHandler(state.detail != null && state.plan == null) { model.closeItem() }
    Scaffold(
        topBar = {
            TopAppBar(title = {
                Column {
                    Text("MAX Postiz")
                    Text(if (state.profile?.testLoopback == true) "Стенд · ${caps.environment}" else caps.environment,
                        style = MaterialTheme.typography.labelMedium)
                }
            }, actions = {
                TextButton(onClick = lock) { Text("Закрыть") }
                TextButton(onClick = { model.logout() }, enabled = !state.busy) { Text("Выход") }
            })
        },
        bottomBar = {
            NavigationBar {
                Section.entries.forEach { section ->
                    NavigationBarItem(selected = section == state.section, enabled = !state.busy && state.plan == null,
                        onClick = { model.select(section) }, icon = {
                            Text(when (section) {
                                Section.Overview -> "◉"
                                Section.Queue -> "≡"
                                Section.Calendar -> "▦"
                                Section.Audit -> "◷"
                            })
                        }, label = { Text(section.label) }, modifier = Modifier.testTag("tab_${section.name}"))
                }
            }
        }
    ) { insets ->
        PageColumn(Modifier.padding(insets), state.section to state.detail?.id) {
            if (state.busy) LinearProgressIndicator(Modifier.fillMaxWidth())
            state.error?.let { Notice(it, true) }
            if (state.offline) {
                val observed = stamp(state.lastReadAt?.toString(), state.zone)
                Notice(
                    if (observed == null) "Данные ещё не получены. Повторите чтение."
                    else "Показан снимок на $observed. Команды станут доступны после обновления."
                )
            }
            if (state.reauth && state.profile?.testLoopback == false) {
                Button(onClick = { model.reconnect(openBrowser) }, enabled = !state.busy) { Text("Войти снова") }
                if (model.rememberedUsername(state.profile) != null) {
                    OutlinedButton(onClick = { model.reconnect(openBrowser, copyPassword) }, enabled = !state.busy) {
                        Text("Скопировать пароль и войти снова")
                    }
                }
            }
            state.pending?.let { OperationStatus(state, model, it.id) }
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically) {
                if (state.detail != null) TextButton(onClick = model::closeItem) { Text("К списку") }
                else Text(state.section.label, style = MaterialTheme.typography.headlineSmall)
                FilledTonalButton(onClick = model::refresh, enabled = !state.busy) { Text("Обновить") }
            }
            val detail = state.detail
            if (detail != null) Detail(detail, state, model)
            else when (state.section) {
                Section.Overview -> Overview(state, model)
                Section.Queue, Section.Calendar -> Queue(state, model)
                Section.Audit -> Audit(state)
            }
            if (state.detail == null && state.cursor != null) {
                OutlinedButton(onClick = { model.more() }, enabled = !state.busy) { Text("Загрузить ещё") }
            }
        }
    }
    state.plan?.let { PlanDialog(state, model, it) }
}

@Composable
private fun Overview(state: PanelState, model: PanelViewModel) {
    val overview = state.overview
    val reserve = state.reserve
    val caps = requireNotNull(state.capabilities)
    val paused = overview?.optBoolean("paused", true)
    val action = if (paused == true) "resume_project" else "pause_project"
    Button(onClick = { model.select(Section.Queue) }, enabled = !state.busy,
        modifier = Modifier.fillMaxWidth()) { Text("Открыть очередь") }
    CommandCard("Управление проектом", mapOf(action to (action in overview?.strings("available_actions").orEmpty())),
        state, overview?.strings("blocked_reasons")?.firstOrNull()?.let(::blocker)) { model.plan(it, "main") }
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            Text(when {
                overview == null -> "Ожидается снимок проекта"
                overview.optBoolean("paused", true) -> "Проект на паузе"
                else -> "Проект работает"
            }, style = MaterialTheme.typography.titleLarge)
            Field("Состояние", status(overview?.string("phase")))
            Field("Обновление сервера", stamp(overview?.string("observed_at"), state.zone))
            Field("Сигнал отправителя", stamp(overview?.string("heartbeat_at"), state.zone))
            Field("Сигнал подготовки", stamp(overview?.optJSONObject("preparation")?.string("heartbeat_at"), state.zone))
            Field("Следующий проход", stamp(overview?.string("next_run_at"), state.zone))
            Field("Неясные отправки", overview?.string("unresolved_dispatches"))
            Field("Резерв", reserve?.let { "${it.optInt("ready")} из ${it.optInt("target")}" })
            Field("Последняя доставка", stamp(reserve?.string("last_delivered_at"), state.zone))
            Field("Шлюз", listOfNotNull(caps.version, caps.role?.let(::status)).joinToString(" · "))
            overview?.strings("blocked_reasons").orEmpty().forEach { Text(blocker(it)) }
        }
    }
    Text("Адресаты", style = MaterialTheme.typography.titleLarge)
    state.targets.forEach { target ->
        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Text("${platform(target.platform)} · ${target.label}", style = MaterialTheme.typography.titleMedium)
                Text(if (!target.enabled) "Отключён" else if (target.paused) "На паузе" else "Активен")
                target.blockers.forEach { Text(blocker(it)) }
                val targetAction = if (target.paused) "resume_target" else "pause_target"
                FilledTonalButton(onClick = { model.plan(targetAction, target.id) },
                    enabled = targetAction in target.actions && targetAction in caps.actions && state.mutationsAvailable,
                    modifier = Modifier.fillMaxWidth().testTag(targetAction)) {
                    Text(requireNotNull(actionName(targetAction)))
                }
            }
        }
    }
}

@Composable
private fun Audit(state: PanelState) {
    if (state.history.isEmpty() && !state.busy) Text("Журнал команд пуст.")
    state.history.forEach { entry ->
        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Text(actionName(entry.string("action") ?: "") ?: entry.string("action") ?: "Операция",
                    style = MaterialTheme.typography.titleMedium)
                Field("Статус", status(entry.string("state")))
                Field("Время", stamp(entry.string("updated_at") ?: entry.string("created_at"), state.zone))
                Field("Оператор", entry.string("actor"))
                Field("ID", entry.string("operation_id") ?: entry.string("id"))
                Field("Причина", entry.string("reason"))
                Field("Код", entry.string("code"))
            }
        }
    }
}

@Composable
internal fun PageColumn(modifier: Modifier = Modifier, scrollKey: Any? = null, content: @Composable () -> Unit) {
    val scroll = key(scrollKey) { rememberScrollState() }
    Box(modifier.fillMaxSize(), contentAlignment = Alignment.TopCenter) {
        Column(Modifier.widthIn(max = 840.dp).fillMaxWidth().verticalScroll(scroll).padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp)) { content() }
    }
}

@Composable
internal fun Notice(message: String, error: Boolean = false) {
    Text(message, color = if (error) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onSurfaceVariant)
}

@Composable
internal fun Field(label: String, value: String?) {
    if (!value.isNullOrBlank()) Text("$label · $value")
}

@Composable
internal fun <T> ChoiceMenu(label: String, choices: List<Pair<String, T>>, enabled: Boolean = true,
                            onSelect: (T) -> Unit) {
    var open by remember { mutableStateOf(false) }
    Box {
        OutlinedButton(onClick = { open = true }, enabled = enabled) { Text(label) }
        DropdownMenu(expanded = open, onDismissRequest = { open = false }) {
            choices.forEach { (name, value) ->
                DropdownMenuItem(text = { Text(name) }, onClick = {
                    open = false
                    onSelect(value)
                })
            }
        }
    }
}
