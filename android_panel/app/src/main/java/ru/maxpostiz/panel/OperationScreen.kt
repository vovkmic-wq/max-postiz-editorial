package ru.maxpostiz.panel

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp

@Composable
internal fun OperationStatus(state: PanelState, model: PanelViewModel, id: String) {
    val receipt = state.receipt
    val result = receipt?.result
    Card(Modifier.fillMaxWidth().testTag("operation_status")) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            Text("Команда", style = MaterialTheme.typography.titleMedium)
            Text(status(receipt?.state) ?: "Ожидается ответ шлюза")
            Field("Идентификатор", id)
            Field("Результат", status(result?.string("outcome") ?: result?.string("state")))
            Field("Доказательство", status(result?.string("evidence_kind")))
            receipt?.code?.let { Notice(blocker(it), true) }
            if (receipt?.needsStatus != false) {
                OutlinedButton(onClick = model::checkStatus, enabled = !state.busy) { Text("Проверить команду") }
                if (state.canResubmit && !state.reauth && state.pending?.isPlanCurrent() == true) {
                    OutlinedButton(onClick = model::submitSameCommand, enabled = !state.busy) {
                        Text("Повторить тот же запрос")
                    }
                }
            } else {
                OutlinedButton(onClick = model::acknowledge, enabled = !state.busy) { Text("Закрыть квитанцию") }
            }
        }
    }
}

@Composable
internal fun PlanDialog(state: PanelState, model: PanelViewModel, plan: OperationPlan) {
    var reason by rememberSaveable(plan.id) {
        mutableStateOf(if (plan.action == "reconcile_dispatch") "Сверка неизвестного исхода" else "")
    }
    val objectName = when (plan.action) {
        "pause_project", "resume_project" -> "Весь проект"
        "pause_target", "resume_target" -> targetName(plan.objectId,
            state.targets.find { it.id == plan.objectId }?.platform)
        else -> plan.objectId
    }
    AlertDialog(
        onDismissRequest = model::cancelPlan,
        title = { Text(actionName(plan.action) ?: "Подтвердить действие") },
        text = {
            Column(Modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Field("Сервер", state.capabilities?.let { environmentName(it.environment, state.profile?.testLoopback == true) })
                Field("Адрес сервера", state.profile?.origin)
                Field("Объект", objectName)
                Field("Действие", actionName(plan.effect) ?: status(plan.effect))
                Field("План действует до", stamp(plan.expiresAt, state.zone))
                if (plan.action == "retire_preapproval_no_send") {
                    Notice("Сервер сверит черновик в Postiz. Если проверка пройдёт, слот закроется, а черновик и история сохранятся.")
                }
                if (plan.action == "reconcile_dispatch") Notice("Сервер проверит результат прежней отправки.")
                OutlinedTextField(reason, { reason = it }, label = { Text("Обоснование") }, singleLine = true,
                    modifier = Modifier.fillMaxWidth().testTag("operation_reason"))
            }
        },
        dismissButton = { TextButton(onClick = model::cancelPlan) { Text("Отмена") } },
        confirmButton = {
            Button(enabled = state.mutationsAvailable && reason.trim().length in 5..500 && !plan.isExpired(),
                onClick = { model.confirm(reason) }, modifier = Modifier.testTag("confirm_operation")) {
                Text("Подтвердить")
            }
        },
    )
}

@Composable
internal fun CommandCard(title: String, commands: Map<String, Boolean>, state: PanelState,
                         blockerMessage: String? = null, invoke: (String) -> Unit) {
    val allowed = state.capabilities?.actions.orEmpty()
    val explanation = when {
        state.capabilities?.environment?.endsWith("-readonly") == true ->
            "Режим чтения. Изменения на этом сервере недоступны."
        allowed.isEmpty() -> "У этого подключения есть доступ к просмотру."
        state.offline -> "Обновите данные после восстановления связи."
        state.reauth -> "Войдите снова, чтобы выполнять команды."
        state.pending != null -> "Сначала проверьте квитанцию предыдущей команды."
        commands.values.none { it } && !state.busy -> blockerMessage ?: "Команды недоступны для текущего состояния."
        else -> null
    }
    Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant),
        modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(title, style = MaterialTheme.typography.titleLarge)
            explanation?.let { Notice(it) }
            commands.forEach { (action, available) ->
                actionName(action)?.let { label ->
                    FilledTonalButton(onClick = { invoke(action) },
                        enabled = available && action in allowed && state.mutationsAvailable,
                        modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp).testTag(action)) { Text(label) }
                }
            }
        }
    }
}

internal fun actionName(value: String): String? = when (value) {
    "pause_project" -> "Пауза проекта"
    "resume_project" -> "Возобновить проект"
    "pause_target" -> "Пауза адресата"
    "resume_target" -> "Возобновить адресата"
    "hold_item" -> "Заблокировать запись"
    "release_item_hold" -> "Снять блокировку"
    "reconcile_dispatch" -> "Сверить исход"
    "retire_preapproval_no_send" -> "Закрытие просроченного слота"
    else -> null
}
