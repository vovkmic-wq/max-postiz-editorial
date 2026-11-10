package ru.maxpostiz.panel

import android.app.KeyguardManager
import android.content.ClipData
import android.content.ClipDescription
import android.content.ClipboardManager
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.PersistableBundle
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.selection.toggleable
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.ExperimentalComposeUiApi
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.testTagsAsResourceId
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import java.util.UUID

class MainActivity : ComponentActivity() {
    private val model by viewModels<PanelViewModel>()
    private val access by viewModels<LocalAccess>()
    private var clipboardMarker: String? = null
    private var leftAfterCopy = false

    @OptIn(ExperimentalComposeUiApi::class)
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (!BuildConfig.DEBUG) window.addFlags(WindowManager.LayoutParams.FLAG_SECURE)
        enableEdgeToEdge()
        setContent {
            val state by model.state.collectAsStateWithLifecycle()
            val localAccess by access.state.collectAsStateWithLifecycle()
            val browser = rememberLauncherForActivityResult(ActivityResultContracts.StartActivityForResult()) {
                model.browserResult(it.data)
            }
            val reset = rememberLauncherForActivityResult(ActivityResultContracts.StartActivityForResult()) {
                if (it.resultCode == RESULT_OK) access.resetAfterDeviceConfirmation()
            }
            val openBrowser: (Intent) -> Unit = { browser.launch(it) }
            val keyguard = getSystemService(KeyguardManager::class.java)
            LaunchedEffect(localAccess.unlocked) {
                if (localAccess.unlocked && (!BuildConfig.PRIVATE_LAN || Build.VERSION.SDK_INT < 37 ||
                        checkSelfPermission("android.permission.ACCESS_LOCAL_NETWORK") == PackageManager.PERMISSION_GRANTED)) {
                    model.resumeSavedSession()
                }
            }
            MaterialTheme(colorScheme = if (isSystemInDarkTheme()) darkColorScheme() else lightColorScheme()) {
                Surface(Modifier.fillMaxSize().semantics { testTagsAsResourceId = true }) {
                    if (!localAccess.unlocked) {
                        val recover = keyguard?.takeIf { it.isDeviceSecure }?.let { manager ->
                            val recoverPin: () -> Unit = { deviceConfirmation(manager)?.let { reset.launch(it) } }
                            recoverPin
                        }
                        PinScreen(localAccess, access::submit, recover)
                    } else if (state.capabilities == null) {
                        ConnectionScreen(state, openBrowser)
                    } else {
                        PanelScreen(state, model, openBrowser, ::copyPassword, ::lockPanel)
                    }
                }
            }
        }
    }

    override fun onStop() {
        leftAfterCopy = clipboardMarker != null
        if (!isChangingConfigurations) lockPanel()
        super.onStop()
    }

    override fun onUserLeaveHint() {
        lockPanel()
        super.onUserLeaveHint()
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus && leftAfterCopy) clearCopiedPassword()
    }

    private fun copyPassword(password: String) {
        val clipboard = getSystemService(ClipboardManager::class.java) ?: return
        val marker = UUID.randomUUID().toString()
        val clip = ClipData.newPlainText("Пароль MAX Postiz", password)
        clip.description.extras = PersistableBundle().apply {
            putBoolean(ClipDescription.EXTRA_IS_SENSITIVE, true)
            putString(CLIP_MARKER, marker)
        }
        clipboard.setPrimaryClip(clip)
        clipboardMarker = marker
        leftAfterCopy = false
        window.decorView.postDelayed({ if (hasWindowFocus()) clearCopiedPassword() }, 30_000)
    }

    private fun clearCopiedPassword() {
        val marker = clipboardMarker ?: return
        val clipboard = getSystemService(ClipboardManager::class.java) ?: return
        if (clipboard.primaryClipDescription?.extras?.getString(CLIP_MARKER) == marker) {
            clipboard.clearPrimaryClip()
        }
        clipboardMarker = null
        leftAfterCopy = false
    }

    private fun lockPanel() {
        model.cancelPlan()
        access.lock()
    }

    @Suppress("DEPRECATION")
    private fun deviceConfirmation(keyguard: KeyguardManager): Intent? =
        keyguard.createConfirmDeviceCredentialIntent("Сброс PIN MAX Postiz", "Подтвердите блокировку телефона.")

    @Composable
    private fun ConnectionScreen(state: PanelState, openBrowser: (Intent) -> Unit) {
        val saved = if (BuildConfig.PRIVATE_LAN) ConnectionProfile.privateLan else state.profiles.firstOrNull()
        var origin by rememberSaveable { mutableStateOf(saved?.origin.orEmpty()) }
        var issuer by rememberSaveable { mutableStateOf(saved?.issuer.orEmpty()) }
        var clientId by rememberSaveable { mutableStateOf(saved?.clientId.orEmpty()) }
        var rememberLogin by rememberSaveable { mutableStateOf(saved?.let(model::remembersLogin) ?: false) }
        var savedUsername by remember { mutableStateOf(saved?.let(model::rememberedUsername)) }
        var username by remember { mutableStateOf(savedUsername.orEmpty()) }
        var password by remember { mutableStateOf("") }
        var details by rememberSaveable { mutableStateOf(false) }
        var fixture by rememberSaveable { mutableStateOf(saved?.testLoopback == true) }
        var local by rememberSaveable { mutableStateOf(false) }
        var token by remember { mutableStateOf("") }
        var message by remember { mutableStateOf<String?>(null) }
        var afterPermission by remember { mutableStateOf<(() -> Unit)?>(null) }
        val permission = "android.permission.ACCESS_LOCAL_NETWORK"
        val permissionRequest = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            if (granted) afterPermission?.invoke()
            else message = "Доступ к локальной сети отклонён."
            afterPermission = null
        }

        fun start(copy: Boolean) {
            message = null
            val profile = runCatching { ConnectionProfile.from(origin, issuer, clientId, fixture) }
                .getOrElse { message = it.message; return }
            if (fixture && token.isBlank()) {
                message = "Введите тестовый токен стенда."
                return
            }
            val name = username.trim()
            if (!fixture && rememberLogin && (password.isNotEmpty() || name != savedUsername.orEmpty()) &&
                (name.isBlank() || password.isEmpty())) {
                message = "Чтобы сохранить данные входа, укажите имя и пароль."
                return
            }
            val login = if (!fixture && rememberLogin && name.isNotBlank() && password.isNotEmpty()) {
                SavedLogin(name, password)
            } else null
            val connect = {
                model.connect(profile, token.trim(), rememberLogin, login,
                    if (copy) ::copyPassword else null, openBrowser)
                token = ""
                password = ""
                if (login != null) savedUsername = login.username
            }
            if ((BuildConfig.PRIVATE_LAN || local) && !fixture && Build.VERSION.SDK_INT >= 37 &&
                checkSelfPermission(permission) != PackageManager.PERMISSION_GRANTED) {
                afterPermission = connect
                permissionRequest.launch(permission)
            } else connect()
        }

        PageColumn(Modifier.systemBarsPadding().padding(top = 32.dp)) {
            Text("MAX Postiz", style = MaterialTheme.typography.headlineLarge)
            Text("Подключение к шлюзу", style = MaterialTheme.typography.titleLarge)
            if (!BuildConfig.PRIVATE_LAN && state.profiles.isNotEmpty()) {
                ChoiceMenu("Сохранённые серверы", state.profiles.map { it.origin to it }) { profile ->
                    origin = profile.origin
                    issuer = profile.issuer
                    clientId = profile.clientId
                    fixture = profile.testLoopback
                    rememberLogin = model.remembersLogin(profile)
                    savedUsername = model.rememberedUsername(profile)
                    username = savedUsername.orEmpty()
                    password = ""
                    token = ""
                }
            }
            if (BuildConfig.DEBUG) {
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically) {
                    Text("Изолированный стенд")
                    Switch(checked = fixture, enabled = !state.busy, onCheckedChange = {
                        fixture = it
                        token = ""
                        message = null
                        if (it && !origin.startsWith("http://127.0.0.1:")) origin = "http://127.0.0.1:18093"
                        if (!it && origin.startsWith("http://")) origin = ""
                    }, modifier = Modifier.testTag("fixture_mode"))
                }
            }
            if (BuildConfig.PRIVATE_LAN) {
                Field("Шлюз", origin)
                TextButton(onClick = { details = !details }) {
                    Text(if (details) "Скрыть параметры" else "Параметры подключения")
                }
            } else TextInput(origin, { origin = it }, "Адрес шлюза", "gateway_origin", KeyboardType.Uri)
            if (fixture) {
                OutlinedTextField(token, { token = it }, label = { Text("Тестовый токен") },
                    visualTransformation = PasswordVisualTransformation(), singleLine = true,
                    modifier = Modifier.fillMaxWidth().testTag("fixture_token"))
                Text("Подключение через локальный туннель adb.")
            } else {
                if (!BuildConfig.PRIVATE_LAN || details) {
                    TextInput(issuer, { issuer = it }, "Издатель OIDC", "oidc_issuer", KeyboardType.Uri, BuildConfig.PRIVATE_LAN)
                    TextInput(clientId, { clientId = it }, "ID клиента OIDC", "oidc_client", readOnly = BuildConfig.PRIVATE_LAN)
                }
                Row(Modifier.fillMaxWidth().heightIn(min = 48.dp).testTag("remember_login").toggleable(
                    value = rememberLogin, enabled = !state.busy, role = Role.Checkbox,
                    onValueChange = { rememberLogin = it }), verticalAlignment = Alignment.CenterVertically) {
                    Checkbox(checked = rememberLogin, onCheckedChange = null, enabled = !state.busy)
                    Text("Запомнить имя и пароль", Modifier.padding(start = 12.dp))
                }
                if (rememberLogin) {
                    TextInput(username, { username = it.take(200) }, "Имя пользователя", "remember_username")
                    OutlinedTextField(password, { password = it.take(512) },
                        label = { Text(if (savedUsername == null) "Пароль" else "Новый пароль (необязательно)") },
                        visualTransformation = PasswordVisualTransformation(),
                        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password), singleLine = true,
                        modifier = Modifier.fillMaxWidth().testTag("remember_password"))
                    Notice("Данные шифруются на телефоне. При новом входе имя подставится, а пароль можно вставить из буфера.")
                }
                if (Build.VERSION.SDK_INT >= 37 && !BuildConfig.PRIVATE_LAN) {
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically) {
                        Text("Доступ через локальную сеть", Modifier.weight(1f))
                        Switch(checked = local, onCheckedChange = { local = it })
                    }
                }
            }
            (message ?: state.error)?.let { Notice(it, true) }
            if (state.busy) LinearProgressIndicator(Modifier.fillMaxWidth())
            Button(enabled = !state.busy, modifier = Modifier.fillMaxWidth().testTag("connect"),
                onClick = { start(false) }) { Text(if (fixture) "Подключить стенд" else "Войти") }
            if (!fixture && rememberLogin && username.isNotBlank() &&
                (password.isNotEmpty() || username.trim() == savedUsername)) {
                OutlinedButton(onClick = { start(true) }, enabled = !state.busy, modifier = Modifier.fillMaxWidth()) {
                    Text("Скопировать пароль и открыть вход")
                }
            }
        }
    }

    private companion object { const val CLIP_MARKER = "ru.maxpostiz.panel.CLIP_MARKER" }
}

@Composable
private fun TextInput(value: String, change: (String) -> Unit, label: String, tag: String,
                      type: KeyboardType = KeyboardType.Text, readOnly: Boolean = false) {
    OutlinedTextField(value, change, label = { Text(label) }, singleLine = true, readOnly = readOnly,
        keyboardOptions = KeyboardOptions(keyboardType = type), modifier = Modifier.fillMaxWidth().testTag(tag))
}
