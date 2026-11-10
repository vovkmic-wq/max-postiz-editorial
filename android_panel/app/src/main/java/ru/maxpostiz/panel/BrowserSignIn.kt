package ru.maxpostiz.panel

import android.content.Context
import android.content.Intent
import android.content.pm.verify.domain.DomainVerificationManager
import android.content.pm.verify.domain.DomainVerificationUserState
import android.net.Uri
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException
import kotlinx.coroutines.suspendCancellableCoroutine
import net.openid.appauth.AppAuthConfiguration
import net.openid.appauth.AuthState
import net.openid.appauth.AuthorizationException
import net.openid.appauth.AuthorizationRequest
import net.openid.appauth.AuthorizationResponse
import net.openid.appauth.AuthorizationService
import net.openid.appauth.AuthorizationServiceConfiguration
import net.openid.appauth.ResponseTypeValues
import net.openid.appauth.connectivity.ConnectionBuilder

internal class BrowserSignIn(private val context: Context) : AutoCloseable {
    private val connectionBuilder = ConnectionBuilder { uri ->
        check(isAllowedEndpoint(uri)) {
            "Для входа нужен проверенный HTTPS."
        }
        (URL(uri.toString()).openConnection() as HttpURLConnection).apply {
            instanceFollowRedirects = false
            connectTimeout = 8_000
            readTimeout = 15_000
        }
    }
    private val service = AuthorizationService(context,
        AppAuthConfiguration.Builder().setConnectionBuilder(connectionBuilder).build())

    suspend fun start(profile: ConnectionProfile, loginHint: String?, remember: Boolean): Pair<Intent, String> {
        val redirect = Uri.parse(BuildConfig.REDIRECT_URI)
        if (!BuildConfig.PRIVATE_LAN) {
            val verification = context.getSystemService(DomainVerificationManager::class.java)
                ?.getDomainVerificationUserState(context.packageName)
            if (verification?.isLinkHandlingAllowed != true ||
                verification.hostToStateMap[redirect.host] != DomainVerificationUserState.DOMAIN_STATE_VERIFIED) {
                throw IOException("HTTPS-ссылка возврата не подтверждена для этой сборки.")
            }
        }
        val configuration = discover(profile)
        val request = AuthorizationRequest.Builder(configuration, profile.clientId, ResponseTypeValues.CODE, redirect)
            .setScope(if (!BuildConfig.PRIVATE_LAN || remember && BuildConfig.PERSISTENT_LAN_SESSION)
                "openid offline_access" else "openid")
            .setPrompt("login")
            .setLoginHint(loginHint)
            .setAdditionalParameters(mapOf("max_age" to "0"))
            .build()
        check(request.codeVerifierChallengeMethod == "S256" && !request.nonce.isNullOrBlank())
        return service.getAuthorizationRequestIntent(request) to requireNotNull(request.state)
    }

    suspend fun finish(result: Intent, profile: ConnectionProfile, expectedState: String): AuthState {
        val response = AuthorizationResponse.fromIntent(result)
            ?: throw IOException("Вход отменён или ответ сервера не принят.")
        val request = response.request
        if (request.state != expectedState || response.state != expectedState ||
            request.clientId != profile.clientId || request.redirectUri.toString() != BuildConfig.REDIRECT_URI ||
            request.configuration.discoveryDoc?.issuer != profile.issuer ||
            request.codeVerifierChallengeMethod != "S256" || request.nonce.isNullOrBlank()) {
            throw IOException("Ответ входа не соответствует текущему запросу.")
        }
        return suspendCancellableCoroutine { continuation ->
            service.performTokenRequest(response.createTokenExchangeRequest()) { token, exception ->
                if (!continuation.isActive) return@performTokenRequest
                if (token?.accessToken.isNullOrBlank() || token?.idToken.isNullOrBlank()) {
                    continuation.resumeWithException(IOException("Сервер входа не выдал действующую сессию.", exception))
                } else {
                    continuation.resume(AuthState(response, token, exception))
                }
            }
        }
    }

    suspend fun accessToken(state: AuthState): String = suspendCancellableCoroutine { continuation ->
        state.performActionWithFreshTokens(service) { accessToken, _, exception ->
            if (!continuation.isActive) return@performActionWithFreshTokens
            when {
                !accessToken.isNullOrBlank() -> continuation.resume(accessToken)
                exception == AuthorizationException.GeneralErrors.NETWORK_ERROR ->
                    continuation.resumeWithException(IOException("OIDC_NETWORK_UNAVAILABLE", exception))
                state.refreshToken == null || exception == AuthorizationException.TokenRequestErrors.INVALID_GRANT ->
                    continuation.resumeWithException(SessionExpired(
                        "Срок входа истёк. Войдите снова.", exception))
                else -> continuation.resumeWithException(IOException("OIDC_REFRESH_UNAVAILABLE", exception))
            }
        }
    }

    fun revoke(profile: ConnectionProfile, refreshToken: String): Boolean {
        val uri = Uri.parse(profile.issuer).buildUpon().appendEncodedPath("protocol/openid-connect/revoke").build()
        return try {
            val connection = connectionBuilder.openConnection(uri)
            try {
                connection.requestMethod = "POST"
                connection.connectTimeout = 3_000
                connection.readTimeout = 4_000
                connection.doOutput = true
                connection.setRequestProperty("Content-Type", "application/x-www-form-urlencoded")
                val body = mapOf("client_id" to profile.clientId, "token" to refreshToken,
                    "token_type_hint" to "refresh_token").entries.joinToString("&") { (key, value) ->
                    "$key=${URLEncoder.encode(value, Charsets.UTF_8.name())}"
                }.encodeToByteArray()
                connection.setFixedLengthStreamingMode(body.size)
                connection.outputStream.use { it.write(body) }
                connection.responseCode == HttpURLConnection.HTTP_OK
            } finally {
                connection.disconnect()
            }
        } catch (_: IOException) {
            false
        }
    }

    override fun close() = service.dispose()

    private suspend fun discover(profile: ConnectionProfile): AuthorizationServiceConfiguration =
        suspendCancellableCoroutine { continuation ->
            AuthorizationServiceConfiguration.fetchFromIssuer(Uri.parse(profile.issuer), { config, exception ->
                if (!continuation.isActive) return@fetchFromIssuer
                if (config?.discoveryDoc?.issuer != profile.issuer ||
                    !isAllowedEndpoint(config.authorizationEndpoint) || !isAllowedEndpoint(config.tokenEndpoint)) {
                    continuation.resumeWithException(IOException("Проверьте HTTPS-адрес и издателя OIDC.", exception))
                } else {
                    continuation.resume(config)
                }
            }, connectionBuilder)
        }

    private fun isAllowedEndpoint(uri: Uri): Boolean =
        uri.scheme == "https" && uri.userInfo == null && uri.fragment == null &&
            (!BuildConfig.PRIVATE_LAN || uri.encodedAuthority == Uri.parse(ConnectionProfile.privateLan.origin).encodedAuthority)
}

internal class SessionExpired(message: String, cause: AuthorizationException?) : IOException(message, cause)
