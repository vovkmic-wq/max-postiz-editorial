package ru.maxpostiz.panel

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import java.security.MessageDigest
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec
import org.json.JSONArray
import org.json.JSONObject

internal data class SavedLogin(val username: String, val password: String) {
    override fun toString() = "SavedLogin([protected])"
}

internal class SessionStore(context: Context) {
    private val preferences = context.getSharedPreferences("mobile-panel", Context.MODE_PRIVATE)
    private val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }

    fun profiles(): List<ConnectionProfile> =
        decrypt("profiles")?.let { serialized ->
            runCatching {
                val values = JSONArray(serialized)
                List(values.length()) { i ->
                    val value = values.getJSONObject(i)
                    ConnectionProfile.from(value.getString("origin"), value.getString("issuer"),
                        value.getString("client"), value.optBoolean("fixture"))
                }
            }.getOrDefault(emptyList())
        }.orEmpty()

    fun remember(profile: ConnectionProfile) {
        val list = (listOf(profile) + profiles().filter { it != profile }).take(8)
        val encoded = JSONArray(list.map { JSONObject()
            .put("origin", it.origin).put("issuer", it.issuer).put("client", it.clientId).put("fixture", it.testLoopback)
        })
        encrypt("profiles", encoded.toString())
    }

    fun loadAuth(profile: ConnectionProfile): String? = decrypt("auth-" + hash(profile))
    fun saveAuth(profile: ConnectionProfile, value: String?) = encrypt("auth-" + hash(profile), value)

    fun remembersLogin(profile: ConnectionProfile): Boolean =
        preferences.getBoolean("remember-" + hash(profile), preferences.contains("auth-" + hash(profile)))

    fun rememberLogin(profile: ConnectionProfile, enabled: Boolean) {
        val edit = preferences.edit().putBoolean("remember-" + hash(profile), enabled)
        if (!enabled) edit.remove("auth-" + hash(profile)).remove("login-" + hash(profile))
        check(edit.commit()) { "Не удалось сохранить выбор входа." }
    }

    fun loadLogin(profile: ConnectionProfile): SavedLogin? = decrypt("login-" + hash(profile))?.let {
        runCatching {
            val data = JSONObject(it)
            SavedLogin(data.getString("username"), data.getString("password"))
        }.getOrNull()
    }

    fun saveLogin(profile: ConnectionProfile, login: SavedLogin) = encrypt("login-" + hash(profile), JSONObject()
        .put("username", login.username).put("password", login.password).toString())

    fun hasPin(): Boolean = preferences.contains(PIN)
    fun loadPin(): String? = decrypt(PIN)
    fun savePin(value: String?) = encrypt(PIN, value)

    fun loadPending(profile: ConnectionProfile, environment: String): String? =
        decrypt(pendingKey(profile, environment))

    fun savePending(profile: ConnectionProfile, environment: String, value: String?) =
        encrypt(pendingKey(profile, environment), value)

    private fun pendingKey(profile: ConnectionProfile, environment: String) = "pending-" + hash(profile, environment)

    private fun hash(profile: ConnectionProfile, environment: String = ""): String =
        Base64.encodeToString(MessageDigest.getInstance("SHA-256").digest(
            JSONArray(listOf(profile.origin, profile.issuer, profile.clientId, environment)).toString().encodeToByteArray()
        ), Base64.NO_WRAP or Base64.URL_SAFE)

    private fun key(): SecretKey {
        (store.getKey(ALIAS, null) as? SecretKey)?.let { return it }
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").apply {
            init(KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).build())
        }.generateKey()
    }

    private fun encrypt(name: String, value: String?) {
        val edit = preferences.edit()
        if (value == null) {
            edit.remove(name)
        } else {
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(Cipher.ENCRYPT_MODE, key())
            cipher.updateAAD(name.encodeToByteArray())
            val encoded = (cipher.iv + cipher.doFinal(value.encodeToByteArray()))
            edit.putString(name, Base64.encodeToString(encoded, Base64.NO_WRAP))
        }
        check(edit.commit()) { "Не удалось сохранить состояние приложения." }
    }

    private fun decrypt(name: String): String? {
        val serialized = preferences.getString(name, null) ?: return null
        return runCatching {
            val encoded = Base64.decode(serialized, Base64.NO_WRAP)
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, encoded.copyOfRange(0, 12)))
            cipher.updateAAD(name.encodeToByteArray())
            cipher.doFinal(encoded.copyOfRange(12, encoded.size)).decodeToString(throwOnInvalidSequence = true)
        }.getOrNull()
    }

    private companion object {
        const val ALIAS = "max-postiz-panel-v1"
        const val PIN = "local-pin-v1"
    }
}
