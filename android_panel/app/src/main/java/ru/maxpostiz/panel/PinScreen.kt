package ru.maxpostiz.panel

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.delay

@Composable
internal fun PinScreen(state: AccessState, submit: (String, String) -> Unit, reset: (() -> Unit)?) {
    var pin by remember(state.configured) { mutableStateOf("") }
    var confirm by remember(state.configured) { mutableStateOf("") }
    var now by remember { mutableLongStateOf(System.currentTimeMillis()) }
    LaunchedEffect(state.retryAt) {
        now = System.currentTimeMillis()
        while (now < state.retryAt) {
            delay(250)
            now = System.currentTimeMillis()
        }
    }
    val seconds = ((state.retryAt - now + 999) / 1000).coerceAtLeast(0)
    val ready = !state.busy && seconds == 0L && validPin(pin) && (state.configured || validPin(confirm))
    val apply = {
        if (ready) {
            submit(pin, confirm)
            pin = ""
            confirm = ""
        }
    }
    PageColumn(Modifier.systemBarsPadding().imePadding().padding(top = 24.dp)) {
        Text("MAX Postiz", style = MaterialTheme.typography.headlineLarge)
        Text(if (state.configured) "Разблокировать панель" else "Настройте защиту панели",
            style = MaterialTheme.typography.headlineSmall)
        Notice(if (state.configured) "Введите PIN приложения из четырёх цифр."
               else "Придумайте PIN из четырёх цифр. Он будет запрашиваться перед входом в панель.")
        PinInput("PIN-код", "local_pin", pin, { pin = it }, !state.busy && seconds == 0L, apply)
        if (!state.configured) PinInput("Повторите PIN-код", "local_pin_confirmation", confirm,
            { confirm = it }, !state.busy, apply)
        state.message?.let { Notice(it, error = true) }
        if (seconds > 0) Notice("Следующая попытка через $seconds сек.", error = true)
        Button(onClick = apply, enabled = ready, modifier = Modifier.fillMaxWidth().testTag("local_pin_submit")) {
            Text(if (state.configured) "Разблокировать" else "Сохранить PIN")
        }
        if (state.configured && reset != null) {
            TextButton(onClick = reset, enabled = !state.busy) { Text("Сбросить через защиту телефона") }
        }
    }
}

@Composable
private fun PinInput(label: String, tag: String, value: String, change: (String) -> Unit,
                     enabled: Boolean, done: () -> Unit) {
    OutlinedTextField(value, { change(it.filter { ch -> ch in '0'..'9' }.take(4)) },
        label = { Text(label) }, singleLine = true, enabled = enabled,
        visualTransformation = PasswordVisualTransformation(),
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.NumberPassword, imeAction = ImeAction.Done),
        keyboardActions = KeyboardActions(onDone = { done() }), modifier = Modifier.fillMaxWidth().testTag(tag))
}
