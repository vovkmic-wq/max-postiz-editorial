package ru.maxpostiz.panel

import android.net.Uri
import java.io.EOFException
import java.io.IOException
import java.net.HttpURLConnection
import java.net.SocketException
import java.net.SocketTimeoutException
import java.net.UnknownHostException
import java.net.URI
import java.net.URL
import java.time.Instant
import kotlin.random.Random
import org.json.JSONArray
import org.json.JSONObject

internal data class ConnectionProfile(val origin: String, val issuer: String, val clientId: String) {
    val testLoopback: Boolean get() = origin.startsWith("http://")

    companion object {
        val privateLan = ConnectionProfile(
            "https://10.20.30.45",
            "https://10.20.30.45/realms/max-postiz-mobile",
            "max-postiz-mobile-android",
        )

        fun from(origin: String, issuer: String, clientId: String, fixture: Boolean): ConnectionProfile {
            if (BuildConfig.PRIVATE_LAN) {
                require(!fixture && origin == privateLan.origin && issuer == privateLan.issuer &&
                    clientId == privateLan.clientId) { "Эта сборка подключается только к настроенному шлюзу." }
                return privateLan
            }
            val address = parseHttpsOrFixture(origin.trim(), fixture)
            require(address.rawPath.isNullOrEmpty() || address.rawPath == "/") { "Введите адрес шлюза без пути." }
            if (fixture) {
                require(BuildConfig.DEBUG && address.scheme == "http" && address.host == "127.0.0.1" && address.port > 0) {
                    "Стенд подключается к 127.0.0.1 через adb reverse."
                }
                return ConnectionProfile(address.toString().trimEnd('/'), "", "")
            }
            val identity = parseHttpsOrFixture(issuer.trim(), false)
            require(clientId.trim().isNotEmpty()) { "Укажите ID клиента OIDC." }
            return ConnectionProfile(address.toString().trimEnd('/'), identity.toString(), clientId.trim())
        }

        private fun parseHttpsOrFixture(value: String, fixture: Boolean): URI {
            val uri = runCatching { URI(value) }.getOrNull()
            require(uri?.host != null && uri.rawUserInfo == null && uri.rawQuery == null && uri.rawFragment == null) {
                "Проверьте адрес сервера."
            }
            require(uri.scheme == "https" || fixture && BuildConfig.DEBUG && uri.scheme == "http") {
                "Для сервера нужен HTTPS."
            }
            require(uri.port == -1 || uri.port in 1..65535) { "Проверьте порт сервера." }
            return uri
        }
    }
}

internal class ApiFailure(
    val status: Int,
    val code: String,
    val correlationId: String?,
    val retryAfter: String? = null,
) : IOException(code)

internal data class Capabilities(
    val environment: String,
    val version: String?,
    val role: String?,
    val zone: String,
    val serverTime: String?,
    val actions: Set<String>,
) {
    companion object {
        fun parse(value: JSONObject): Capabilities {
            if (value.optInt("api_version", -1) != 1) throw IOException("Версия API несовместима.")
            return Capabilities(
                value.getString("environment_id"), value.string("server_version"), value.string("role"),
                value.string("project_timezone") ?: "Europe/Moscow", value.string("server_time"), value.strings("actions"),
            )
        }
    }
}

internal data class QueueItem(
    val id: String,
    val title: String,
    val targetId: String?,
    val scheduledAt: String?,
    val revision: String?,
    val snapshotHash: String?,
    val editorialState: String?,
    val postizState: String?,
    val dispatchState: String?,
    val delivery: JSONObject?,
    val blockers: Set<String>,
    val actions: Set<String>,
    val dispatchId: String?,
    val recordId: String?,
    val details: JSONObject,
) {
    val needsReview: Boolean
        get() = dispatchState in setOf("sending", "unknown") ||
            editorialState in setOf("blocked_unknown", "needs_attention") ||
            details.string("slot_state") == "blocked_unknown" ||
            actions.any { it == "reconcile_dispatch" || it == "retire_preapproval_no_send" }

    companion object {
        fun parse(value: JSONObject) = QueueItem(
            value.getString("id"), value.string("title") ?: "Запись очереди",
            value.string("target_id"), value.string("scheduled_at"), value.string("revision"), value.string("snapshot_hash"),
            value.string("editorial_state"), value.string("postiz_state"), value.string("dispatch_state"),
            value.optJSONObject("delivery_evidence"), value.strings("blocked_reasons"), value.strings("available_actions"),
            value.string("dispatch_id"), value.string("record_id"), value,
        )
    }
}

internal data class Target(val id: String, val label: String, val platform: String?, val paused: Boolean,
                           val enabled: Boolean, val actions: Set<String>, val blockers: Set<String>) {
    companion object {
        fun parse(value: JSONObject) = Target(
            value.getString("id"), value.string("label") ?: value.getString("id"), value.string("platform"),
            value.optBoolean("paused", true), value.optBoolean("enabled", false), value.strings("available_actions"),
            value.strings("blocked_reasons"),
        )
    }
}

internal data class Page<T>(val items: List<T>, val next: String?, val warning: String? = null)

internal data class OperationPlan(
    val id: String, val hash: String, val action: String, val objectId: String,
    val effect: String, val expiresAt: String?,
) {
    fun isExpired(): Boolean =
        expiresAt?.let { runCatching { !Instant.parse(it).isAfter(Instant.now()) }.getOrDefault(false) } ?: false
}

internal data class PendingCommand(
    val id: String,
    val planId: String,
    val planHash: String,
    val reason: String,
    val expiresAt: String? = null,
) {
    fun json(): JSONObject = JSONObject().put("operation_id", id).put("plan_id", planId)
        .put("plan_hash", planHash).put("confirmed", true).put("reason", reason)

    fun storedJson(): JSONObject = json().put("expires_at", expiresAt)

    fun isPlanCurrent(): Boolean = expiresAt?.let {
        runCatching { Instant.parse(it).isAfter(Instant.now()) }.getOrDefault(false)
    } ?: false

    companion object {
        fun parse(value: String?): PendingCommand? = value?.let {
            runCatching {
                val data = JSONObject(it)
                PendingCommand(data.getString("operation_id"), data.getString("plan_id"),
                    data.getString("plan_hash"), data.getString("reason"), data.string("expires_at"))
            }.getOrNull()
        }
    }
}

internal data class Receipt(
    val id: String, val state: String, val code: String?, val result: JSONObject?,
) {
    val needsStatus: Boolean get() = state !in setOf("succeeded", "blocked", "failed")
}

internal class MobileApi(private val profile: ConnectionProfile, private val token: String) {
    fun capabilities(): Capabilities = Capabilities.parse(get("/capabilities"))
    fun overview(): JSONObject = get("/overview")
    fun reserve(): JSONObject = get("/reserve")
    fun targets(): Page<Target> = page(get("/targets"), Target::parse)
    fun queue(calendar: Boolean, filters: Map<String, String>): Page<QueueItem> =
        page(get(if (calendar) "/calendar" else "/queue", filters), QueueItem::parse)
    fun item(id: String): QueueItem = QueueItem.parse(get("/queue/${Uri.encode(id)}"))
    fun dispatch(id: String): QueueItem = QueueItem.parse(get("/dispatches/${Uri.encode(id)}"))
    fun audit(cursor: String? = null): Page<JSONObject> =
        page(get("/audit", cursor?.let { mapOf("cursor" to it) }.orEmpty())) { it }

    fun plan(action: String, objectId: String): OperationPlan {
        val value = request("/operation-plans", JSONObject().put("action", action).put("object_id", objectId))
        if (value.getString("action") != action || value.getString("object_id") != objectId) {
            throw IOException("Ответ шлюза относится к другой команде.")
        }
        return OperationPlan(value.getString("plan_id"), value.getString("plan_hash"), action, objectId,
            value.getString("effect"), value.string("expires_at"))
    }

    fun submit(command: PendingCommand): Receipt =
        receipt(command.id, request("/operations", command.json()))

    fun status(id: String): Receipt = receipt(id, get("/operations/${Uri.encode(id)}"))

    fun statusWithPlan(command: PendingCommand): Receipt = receipt(command.id, get(
        "/operations/${Uri.encode(command.id)}",
        mapOf("plan_id" to command.planId, "plan_hash" to command.planHash),
    ))

    private fun receipt(id: String, value: JSONObject): Receipt {
        if (value.getString("operation_id") != id) throw IOException("Шлюз вернул другой ID операции.")
        return Receipt(id, value.getString("state"), value.string("code"), value.optJSONObject("result"))
    }

    private fun <T> page(value: JSONObject, map: (JSONObject) -> T): Page<T> =
        Page(value.objects("items").map(map), value.string("next_cursor"), value.string("postiz_error"))

    private fun get(path: String, filters: Map<String, String> = emptyMap()): JSONObject {
        val suffix = filters.entries.joinToString("&") { "${Uri.encode(it.key)}=${Uri.encode(it.value)}" }
        val resource = if (suffix.isEmpty()) path else "$path?$suffix"
        return try {
            request(resource)
        } catch (first: IOException) {
            Thread.sleep(retryReadAfter(first) ?: throw first)
            request(resource)
        }
    }

    private fun request(path: String, body: JSONObject? = null): JSONObject {
        val connection = URL("${profile.origin}/mobile/v1$path").openConnection() as HttpURLConnection
        try {
            connection.instanceFollowRedirects = false
            connection.useCaches = false
            connection.connectTimeout = if (body == null) 5_000 else 8_000
            connection.readTimeout = if (body == null) 12_000 else 30_000
            connection.setRequestProperty("Accept", "application/json")
            connection.setRequestProperty("Authorization", "Bearer $token")
            if (body != null) {
                val payload = body.toString().encodeToByteArray()
                connection.requestMethod = "POST"
                connection.doOutput = true
                connection.setRequestProperty("Content-Type", "application/json; charset=utf-8")
                connection.setFixedLengthStreamingMode(payload.size)
                connection.outputStream.use { it.write(payload) }
            }
            val status = connection.responseCode
            val stream = if (status in 200..299) connection.inputStream else connection.errorStream
            val data = stream?.use { input ->
                val bytes = input.readNBytes(2 * 1024 * 1024 + 1)
                if (bytes.size > 2 * 1024 * 1024) throw IOException("Ответ шлюза слишком большой.")
                runCatching { JSONObject(bytes.decodeToString(throwOnInvalidSequence = true)) }.getOrNull()
            }
            if (status !in 200..299) {
                throw ApiFailure(status, data?.string("code")?.takeIf { it.matches(codePattern) } ?: "HTTP_$status",
                    data?.string("correlation_id")?.takeIf { it.matches(idPattern) },
                    connection.getHeaderField("Retry-After"))
            }
            return data ?: throw IOException("Шлюз вернул ответ неизвестного формата.")
        } finally {
            connection.disconnect()
        }
    }

    private fun retryReadAfter(failure: IOException): Long? = when (failure) {
        is ApiFailure -> when {
            failure.status !in transientReadStatuses -> null
            failure.retryAfter != null ->
                failure.retryAfter.trim().toLongOrNull()?.takeIf { it in 0..2 }?.let { it * 1_000 + 200 }
            failure.status == 429 -> null
            else -> Random.nextLong(350, 701)
        }
        is SocketTimeoutException, is SocketException, is UnknownHostException, is EOFException ->
            Random.nextLong(350, 701)
        else -> null
    }

    private companion object {
        val transientReadStatuses = setOf(408, 429, 502, 503, 504)
        val codePattern = Regex("[A-Za-z0-9_]{1,96}")
        val idPattern = Regex("[A-Za-z0-9_-]{1,96}")
    }
}

internal fun JSONObject.string(key: String): String? =
    if (isNull(key)) null else optString(key).takeIf { it.isNotBlank() }

internal fun JSONObject.objects(key: String): List<JSONObject> =
    optJSONArray(key).orEmptyObjects()

private fun JSONArray?.orEmptyObjects(): List<JSONObject> =
    if (this == null) emptyList() else List(length()) { optJSONObject(it) }.filterNotNull()

internal fun JSONObject.strings(key: String): Set<String> = optJSONArray(key)?.let { values ->
    List(values.length()) { values.optString(it) }.filter { it.isNotBlank() }.toSet()
}.orEmpty()
