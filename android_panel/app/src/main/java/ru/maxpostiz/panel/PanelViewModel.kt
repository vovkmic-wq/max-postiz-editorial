package ru.maxpostiz.panel

import android.app.Application
import android.content.Intent
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.util.UUID
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.runInterruptible
import kotlinx.coroutines.withContext
import net.openid.appauth.AuthState
import org.json.JSONObject

internal enum class Section(val label: String) {
    Overview("Обзор"), Queue("Очередь"), Calendar("Календарь"), Audit("История")
}

internal data class PanelState(
    val profiles: List<ConnectionProfile> = emptyList(),
    val profile: ConnectionProfile? = null,
    val capabilities: Capabilities? = null,
    val busy: Boolean = false,
    val offline: Boolean = false,
    val lastReadAt: Instant? = null,
    val error: String? = null,
    val reauth: Boolean = false,
    val section: Section = Section.Overview,
    val overview: JSONObject? = null,
    val reserve: JSONObject? = null,
    val targets: List<Target> = emptyList(),
    val items: List<QueueItem> = emptyList(),
    val history: List<JSONObject> = emptyList(),
    val cursor: String? = null,
    val target: String? = null,
    val reviewOnly: Boolean = false,
    val date: LocalDate? = null,
    val detail: QueueItem? = null,
    val detailDispatch: String? = null,
    val plan: OperationPlan? = null,
    val pending: PendingCommand? = null,
    val canResubmit: Boolean = false,
    val receipt: Receipt? = null,
    val calendarProblem: String? = null,
) {
    val mutationsAvailable: Boolean get() = capabilities != null && lastReadAt != null && !offline && !busy &&
        !reauth && pending == null
    val zone: ZoneId get() = runCatching { ZoneId.of(capabilities?.zone ?: "Europe/Moscow") }
        .getOrDefault(ZoneId.of("Europe/Moscow"))
}

internal class PanelViewModel(application: Application) : AndroidViewModel(application) {
    private val storage = SessionStore(application)
    private val browser = BrowserSignIn(application)
    private val mutable = MutableStateFlow(PanelState(profiles = storage.profiles()))
    val state: StateFlow<PanelState> = mutable
    private var fixtureToken: String? = null
    private var authState: AuthState? = null
    private var browserAttempt: Pair<ConnectionProfile, String>? = null

    fun remembersLogin(profile: ConnectionProfile) = storage.remembersLogin(profile)
    fun rememberedUsername(profile: ConnectionProfile) = storage.loadLogin(profile)?.username

    fun resumeSavedSession() {
        if (state.value.capabilities != null || browserAttempt != null) return
        val profile = if (BuildConfig.PRIVATE_LAN) ConnectionProfile.privateLan
            else state.value.profiles.firstOrNull()?.takeUnless { it.testLoopback } ?: return
        runAction { resumeAuthorized(profile) }
    }

    private suspend fun resumeAuthorized(profile: ConnectionProfile): Boolean {
        val existing = withContext(Dispatchers.IO) {
            storage.loadAuth(profile)?.let { encoded -> runCatching { AuthState.jsonDeserialize(encoded) }.getOrNull() }
        }
        if (existing == null || !existing.isAuthorized) return false
        authState = existing
        try {
            establish(profile)
        } catch (expired: SessionExpired) {
            withContext(Dispatchers.IO) { storage.saveAuth(profile, null) }
            authState = null
            throw expired
        }
        return true
    }

    fun connect(profile: ConnectionProfile, token: String?, remember: Boolean, login: SavedLogin?,
                copyPassword: ((String) -> Unit)?, onLoginStored: (String?) -> Unit, openBrowser: (Intent) -> Unit) =
        connect(profile, token, remember, login, fresh = login != null, copyPassword, onLoginStored, openBrowser)

    private fun connect(profile: ConnectionProfile, token: String?, remember: Boolean, login: SavedLogin?,
                        fresh: Boolean, copyPassword: ((String) -> Unit)?, onLoginStored: ((String?) -> Unit)?,
                        openBrowser: (Intent) -> Unit) {
        runAction {
            if (profile.testLoopback) {
                require(!token.isNullOrBlank() && token.length <= 4096 && token.none(Char::isWhitespace))
                fixtureToken = token
                authState = null
                establish(profile)
            } else {
                fixtureToken = null
                val savedLogin = withContext(Dispatchers.IO) {
                    storage.rememberLogin(profile, remember, login)
                }
                onLoginStored?.invoke(savedLogin?.username)
                if (remember && !fresh) {
                    try {
                        if (resumeAuthorized(profile)) return@runAction
                    } catch (_: SessionExpired) {
                        // Only an explicit sign-in may open the browser after expiration.
                    } catch (error: ApiFailure) {
                        if (error.status != 401) throw error
                    }
                }
                val (intent, attempt) = browser.start(profile, savedLogin?.username, remember)
                browserAttempt = profile to attempt
                if (copyPassword != null && savedLogin != null) copyPassword(savedLogin.password)
                openBrowser(intent)
            }
        }
    }

    fun browserResult(result: Intent?) {
        val (profile, expected) = browserAttempt ?: return
        browserAttempt = null
        if (result == null) {
            mutable.update { it.copy(error = "Вход отменён.") }
            return
        }
        runAction {
            authState = browser.finish(result, profile, expected)
            establish(profile)
        }
    }

    fun reconnect(openBrowser: (Intent) -> Unit, copyPassword: ((String) -> Unit)? = null) {
        val profile = state.value.profile ?: return
        if (!profile.testLoopback) connect(profile, null, remembersLogin(profile), login = null,
            fresh = true, copyPassword, onLoginStored = null, openBrowser)
    }

    private suspend fun establish(profile: ConnectionProfile) {
        val client = api(profile)
        val caps = try {
            runInterruptible(Dispatchers.IO) { client.capabilities() }
        } catch (error: ApiFailure) {
            if (error.status == 401 && !profile.testLoopback) {
                withContext(Dispatchers.IO) { storage.saveAuth(profile, null) }
            }
            throw error
        }
        val (profiles, pending) = withContext(Dispatchers.IO) {
            storage.remember(profile)
            storage.profiles() to PendingCommand.parse(storage.loadPending(profile, caps.environment))
        }
        mutable.value = PanelState(
            profiles = profiles, profile = profile, capabilities = caps, busy = true, pending = pending,
        )
        if (pending != null) loadStatus(pending.id)
        else loadSection(reset = true)
    }

    fun logout() = runAction {
        val profile = state.value.profile
        val refreshToken = authState?.refreshToken
        profile?.let { withContext(Dispatchers.IO) { storage.saveAuth(it, null) } }
        authState = null
        fixtureToken = null
        browserAttempt = null
        mutable.value = PanelState(profiles = storage.profiles(), busy = true)
        if (BuildConfig.PERSISTENT_LAN_SESSION && profile != null && refreshToken != null) {
            val revoked = runInterruptible(Dispatchers.IO) { browser.revoke(profile, refreshToken) }
            if (!revoked) mutable.update {
                it.copy(error = "Сессия удалена с телефона. Не удалось подтвердить её отзыв на сервере.")
            }
        }
    }

    fun select(section: Section) = runAction {
        val view = state.value.copy(section = section, detail = null, detailDispatch = null)
        loadSection(reset = true, view = view)
    }

    fun refresh() = runAction {
        reloadView()
    }

    fun more() = runAction { loadSection(reset = false) }

    fun filterTarget(target: String?) = runAction {
        loadSection(reset = true, view = state.value.copy(target = target))
    }

    fun filterReview(reviewOnly: Boolean) = runAction {
        loadSection(reset = true, view = state.value.copy(reviewOnly = reviewOnly))
    }

    fun calendarOffset(offset: Long) = runAction {
        val view = state.value
        val date = (view.date ?: LocalDate.now(view.zone)).plusDays(offset)
        loadSection(reset = true, view = view.copy(date = date))
    }

    fun showItem(id: String, dispatchId: String?) = runAction {
        val detail = readDetail(id, dispatchId)
        freshSnapshot { it.copy(detail = detail, detailDispatch = dispatchId) }
    }

    private suspend fun readDetail(id: String, dispatchId: String?): QueueItem =
        withApi { if (dispatchId == null) it.item(id) else it.dispatch(dispatchId) }

    fun closeItem() {
        if (!state.value.busy) mutable.update { it.copy(detail = null, detailDispatch = null) }
    }

    fun plan(action: String, objectId: String) {
        if (!state.value.mutationsAvailable || action !in state.value.capabilities?.actions.orEmpty()) return
        runAction {
            val plan = withApi { it.plan(action, objectId) }
            mutable.update { it.copy(plan = plan) }
        }
    }

    fun cancelPlan() = mutable.update { it.copy(plan = null) }

    fun confirm(reason: String) {
        val current = state.value
        val plan = current.plan ?: return
        val profile = current.profile ?: return
        val environment = current.capabilities?.environment ?: return
        if (!current.mutationsAvailable || plan.isExpired() || reason.trim().length !in 5..500) return
        runAction {
            val client = api(profile)
            val command = PendingCommand(UUID.randomUUID().toString(), plan.id, plan.hash, reason.trim(), plan.expiresAt)
            withContext(Dispatchers.IO) { storage.savePending(profile, environment, command.storedJson().toString()) }
            mutable.update { it.copy(plan = null, pending = command, receipt = null, canResubmit = false) }
            submit(client, profile, environment, command)
        }
    }

    fun checkStatus() {
        state.value.pending?.let { command -> runAction { loadStatus(command.id) } }
    }

    fun submitSameCommand() {
        val current = state.value
        val command = current.pending ?: return
        val profile = current.profile ?: return
        val environment = current.capabilities?.environment ?: return
        if (!current.canResubmit || current.reauth || !command.isPlanCurrent()) return
        runAction {
            if (loadStatus(command.id) || !command.isPlanCurrent()) return@runAction
            val client = api(profile)
            mutable.update { it.copy(canResubmit = false, error = null) }
            submit(client, profile, environment, command)
        }
    }

    private suspend fun submit(client: MobileApi, profile: ConnectionProfile, environment: String,
                               command: PendingCommand) {
        try {
            val receipt = runInterruptible(Dispatchers.IO) { client.submit(command) }
            mutable.update { it.copy(receipt = receipt) }
            if (!receipt.needsStatus) reloadView()
        } catch (error: ApiFailure) {
            if (error.status in setOf(403, 404, 409, 422) && error.code in planRejections) {
                withContext(Dispatchers.IO) { storage.savePending(profile, environment, null) }
                mutable.update { it.copy(pending = null) }
            }
            throw error
        }
    }

    private suspend fun loadStatus(id: String): Boolean {
        val receipt = try {
            withApi { it.status(id) }
        } catch (error: ApiFailure) {
            if (!error.isMissingOperation()) throw error
            val current = state.value
            val command = current.pending?.takeIf { it.id == id } ?: return statusMissing(false)
            if (command.isPlanCurrent()) return statusMissing(true)
            try {
                withApi { it.statusWithPlan(command) }
            } catch (proof: ApiFailure) {
                if (proof.isMissingOperation() || proof.status == 422 && proof.code == "MOBILE_FILTER_INVALID") {
                    return statusMissing(false)
                }
                if (proof.status != 410 || proof.code != "MOBILE_OPERATION_NOT_ACCEPTED") throw proof

                val profile = requireNotNull(current.profile)
                val environment = requireNotNull(current.capabilities).environment
                withContext(Dispatchers.IO) { storage.savePending(profile, environment, null) }
                mutable.update {
                    it.copy(pending = null, receipt = null, canResubmit = false,
                        error = "Срок плана истёк. Шлюз не принял эту команду.")
                }
                return true
            }
        }
        mutable.update { it.copy(receipt = receipt, canResubmit = false) }
        if (!receipt.needsStatus) reloadView()
        return true
    }

    private fun ApiFailure.isMissingOperation(): Boolean =
        status == 404 && code == "MOBILE_OPERATION_NOT_FOUND"

    private fun statusMissing(canResubmit: Boolean): Boolean {
        mutable.update {
            it.copy(canResubmit = canResubmit, error = "Шлюз пока не подтвердил команду. Проверьте тот же ID.")
        }
        return false
    }

    fun acknowledge() {
        val current = state.value
        if (current.receipt?.needsStatus != false) return
        val profile = current.profile ?: return
        val environment = current.capabilities?.environment ?: return
        runAction {
            withContext(Dispatchers.IO) { storage.savePending(profile, environment, null) }
            mutable.update { it.copy(pending = null, receipt = null, canResubmit = false) }
        }
    }

    override fun onCleared() {
        browser.close()
        super.onCleared()
    }

    private suspend fun reloadView() {
        val view = state.value
        val item = view.detail
        if (item != null) {
            val detail = readDetail(item.id, view.detailDispatch)
            freshSnapshot { it.copy(detail = detail) }
        } else loadSection(reset = true, view = view)
    }

    private suspend fun loadSection(reset: Boolean, view: PanelState = state.value) {
        val cursor = if (reset) null else view.cursor
        val client = api(requireNotNull(view.profile))
        val updated = when (view.section) {
            Section.Overview -> {
                val (overview, reserve, targets) = runInterruptible(Dispatchers.IO) {
                    Triple(client.overview(), client.reserve(), client.targets().items)
                }
                view.copy(overview = overview, reserve = reserve, targets = targets, cursor = null)
            }
            Section.Queue, Section.Calendar -> {
                val calendar = view.section == Section.Calendar
                val filters = buildMap {
                    cursor?.let { put("cursor", it) }
                    view.target?.let { put("target_id", it) }
                    if (view.reviewOnly) put("state", "unresolved")
                    if (calendar) {
                        val day = view.date ?: LocalDate.now(view.zone)
                        put("from", day.atStartOfDay(view.zone).toInstant().toString())
                        put("to", day.plusDays(1).atStartOfDay(view.zone).toInstant().toString())
                    }
                }
                val page = runInterruptible(Dispatchers.IO) { client.queue(calendar, filters) }
                val items = if (reset) page.items else (view.items + page.items).distinctBy {
                    it.details.string("calendar_entry_id") ?: it.id
                }
                view.copy(items = items, cursor = page.next, calendarProblem = page.warning)
            }
            Section.Audit -> {
                val page = runInterruptible(Dispatchers.IO) { client.audit(cursor) }
                view.copy(history = if (reset) page.items else view.history + page.items, cursor = page.next)
            }
        }
        freshSnapshot { updated }
    }

    private fun freshSnapshot(update: (PanelState) -> PanelState) {
        mutable.update { update(it).copy(offline = false, lastReadAt = Instant.now()) }
    }

    private suspend fun api(profile: ConnectionProfile): MobileApi {
        val token = if (profile.testLoopback) requireNotNull(fixtureToken) else {
            val auth = authState ?: throw SessionExpired("Войдите снова.", null)
            browser.accessToken(auth).also {
                withContext(Dispatchers.IO) {
                    if (storage.remembersLogin(profile)) storage.saveAuth(profile, auth.jsonSerializeString())
                }
            }
        }
        return MobileApi(profile, token)
    }

    private suspend fun <T> withApi(action: (MobileApi) -> T): T {
        val client = api(requireNotNull(state.value.profile))
        return runInterruptible(Dispatchers.IO) { action(client) }
    }

    private fun runAction(action: suspend () -> Unit) {
        if (state.value.busy) return
        mutable.update { it.copy(busy = true, error = null) }
        viewModelScope.launch {
            try {
                action()
            } catch (error: CancellationException) {
                throw error
            } catch (error: Exception) {
                val reauth = error is SessionExpired || error is ApiFailure &&
                    (error.status == 401 || error.code == "MOBILE_REAUTH_REQUIRED")
                val message = when (error) {
                    is ApiFailure -> apiError(error)
                    is SessionExpired -> "Срок входа истёк. Войдите снова."
                    is javax.net.ssl.SSLException -> "Проверьте сертификат и HTTPS-адрес шлюза."
                    is IllegalArgumentException -> "Проверьте параметры подключения."
                    else -> "Нет свежего ответа. Проверьте доступ к шлюзу или повторите чтение."
                }
                val current = state.value
                if (reauth && error is ApiFailure && error.status == 401) {
                    current.profile?.let { profile ->
                        withContext(Dispatchers.IO) { runCatching { storage.saveAuth(profile, null) } }
                    }
                    authState = null
                    fixtureToken = null
                    mutable.value = PanelState(profiles = current.profiles, error = message, busy = true)
                } else mutable.update {
                    it.copy(error = message, reauth = it.reauth || reauth,
                        plan = null, offline = it.capabilities != null, canResubmit = false)
                }
            } finally {
                mutable.update { it.copy(busy = false) }
            }
        }
    }

    private fun apiError(error: ApiFailure): String {
        val message = when {
            error.code in setOf("PREAPPROVAL_POSTIZ_TIMEOUT_UNPROVEN", "PREAPPROVAL_POSTIZ_JOURNAL_INVALID",
                "PREAPPROVAL_RETIREMENT_NOT_SAFE") -> blocker(error.code)
            error.code == "MOBILE_REAUTH_REQUIRED" -> "Для команды нужен свежий вход владельца."
            error.status == 401 -> "Сессия не принята. Войдите снова."
            error.status == 403 -> "У этой сессии нет права на действие."
            error.status == 409 -> "Состояние или план изменились. Обновите карточку."
            error.status == 429 -> "Шлюз ограничил частоту запросов. Повторите чтение позже."
            error.status >= 500 -> "Шлюз или его источник данных недоступен."
            else -> "Шлюз отклонил запрос."
        }
        return listOfNotNull(message, error.code, error.correlationId).joinToString(" · ")
    }

    private companion object {
        val planRejections = setOf(
            "MOBILE_REAUTH_REQUIRED", "MOBILE_CONFIRMATION_REQUIRED",
            "MOBILE_PLAN_CHANGED_OR_EXPIRED", "MOBILE_PLAN_NOT_FOUND",
        )
    }
}
