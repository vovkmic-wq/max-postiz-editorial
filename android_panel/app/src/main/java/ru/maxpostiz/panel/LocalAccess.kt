package ru.maxpostiz.panel

import android.app.Application
import android.util.Base64
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import java.security.MessageDigest
import java.security.SecureRandom
import javax.crypto.SecretKeyFactory
import javax.crypto.spec.PBEKeySpec
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject

internal fun validPin(value: String): Boolean = value.length == 4 && value.all { it in '0'..'9' }

internal data class PinAttempts(val failures: Int = 0, val retryAt: Long = 0) {
    fun failed(now: Long): PinAttempts {
        val count = (failures + 1).coerceAtMost(5)
        return PinAttempts(count, if (count == 5) now + 30_000 else 0)
    }
}

internal data class AccessState(
    val configured: Boolean,
    val unlocked: Boolean = false,
    val busy: Boolean = false,
    val message: String? = null,
    val retryAt: Long = 0,
)

internal class LocalAccess(application: Application) : AndroidViewModel(application) {
    private val store = SessionStore(application)
    private val mutable = MutableStateFlow(AccessState(configured = store.hasPin()))
    val state = mutable.asStateFlow()
    private var generation = 0

    fun lock() {
        generation++
        mutable.value = mutable.value.copy(unlocked = false, message = null)
    }

    fun resetAfterDeviceConfirmation() {
        if (mutable.value.busy) return
        run {
            store.savePin(null)
            AccessState(configured = false)
        }
    }

    fun submit(pin: String, confirmation: String) {
        val state = mutable.value
        if (state.busy || !validPin(pin)) return
        if (!state.configured && pin != confirmation) {
            mutable.value = state.copy(message = "PIN-коды различаются. Введите их снова.")
            return
        }
        run {
            if (store.hasPin()) verify(pin) else {
                val salt = ByteArray(16).also(SecureRandom()::nextBytes)
                save(salt, digest(pin, salt), PinAttempts())
                AccessState(configured = true, unlocked = true)
            }
        }
    }

    private fun run(action: () -> AccessState) {
        val started = generation
        mutable.value = mutable.value.copy(busy = true, message = null)
        viewModelScope.launch {
            val result = withContext(Dispatchers.IO) {
                runCatching(action).getOrElse {
                    AccessState(configured = store.hasPin(), message = "Не удалось прочитать защиту приложения.")
                }
            }
            mutable.value = if (started == generation) result else result.copy(unlocked = false)
        }
    }

    private fun verify(pin: String): AccessState {
        val value = JSONObject(requireNotNull(store.loadPin()))
        val now = System.currentTimeMillis()
        val attempts = PinAttempts(value.getInt("failures"), value.getLong("retryAt"))
        if (now < attempts.retryAt) return AccessState(configured = true, retryAt = attempts.retryAt)
        val salt = Base64.decode(value.getString("salt"), Base64.NO_WRAP)
        val hash = Base64.decode(value.getString("hash"), Base64.NO_WRAP)
        if (MessageDigest.isEqual(hash, digest(pin, salt))) {
            if (attempts.failures != 0) save(salt, hash, PinAttempts())
            return AccessState(configured = true, unlocked = true)
        }
        val failed = attempts.failed(now)
        save(salt, hash, failed)
        return AccessState(configured = true, message = "Неверный PIN-код.", retryAt = failed.retryAt)
    }

    private fun save(salt: ByteArray, hash: ByteArray, attempts: PinAttempts) = store.savePin(JSONObject()
        .put("salt", Base64.encodeToString(salt, Base64.NO_WRAP))
        .put("hash", Base64.encodeToString(hash, Base64.NO_WRAP))
        .put("failures", attempts.failures).put("retryAt", attempts.retryAt).toString())

    private fun digest(pin: String, salt: ByteArray): ByteArray {
        val digits = pin.toCharArray()
        val spec = PBEKeySpec(digits, salt, 120_000, 256)
        digits.fill('\u0000')
        return try {
            SecretKeyFactory.getInstance("PBKDF2WithHmacSHA256").generateSecret(spec).encoded
        } finally {
            spec.clearPassword()
        }
    }
}
