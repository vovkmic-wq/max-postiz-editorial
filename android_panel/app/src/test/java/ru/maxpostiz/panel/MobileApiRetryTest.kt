package ru.maxpostiz.panel

import com.sun.net.httpserver.HttpExchange
import com.sun.net.httpserver.HttpServer
import java.net.InetSocketAddress
import java.time.Instant
import java.util.UUID
import java.util.concurrent.atomic.AtomicInteger
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertThrows
import org.junit.Test

class MobileApiRetryTest {
    @Test
    fun transientReadRetriesOnceAndReturnsTheFreshResponse() {
        val requests = AtomicInteger()
        withGateway("/overview", { exchange ->
            if (requests.incrementAndGet() == 1) exchange.reply(503, """{"code":"UNAVAILABLE"}""")
            else exchange.reply(200, """{"paused":false}""")
        }) { client ->
            assertEquals(false, client.overview().getBoolean("paused"))
            assertEquals(2, requests.get())
        }
    }

    @Test
    fun persistentReadFailureStopsAfterTwoRequests() {
        val requests = AtomicInteger()
        withGateway("/overview", { exchange ->
            requests.incrementAndGet()
            exchange.reply(503, """{"code":"UNAVAILABLE"}""")
        }) { client ->
            val error = assertThrows(ApiFailure::class.java) { client.overview() }
            assertEquals(503, error.status)
            assertEquals(2, requests.get())
        }
    }

    @Test
    fun longRetryAfterAndDefiniteNotFoundDoNotRetry() {
        for (status in listOf(429, 404)) {
            val requests = AtomicInteger()
            withGateway("/overview", { exchange ->
                requests.incrementAndGet()
                if (status == 429) exchange.responseHeaders.add("Retry-After", "30")
                exchange.reply(status, """{"code":"UNAVAILABLE"}""")
            }) { client ->
                val error = assertThrows(ApiFailure::class.java) { client.overview() }
                assertEquals(status, error.status)
                assertEquals(1, requests.get())
            }
        }
    }

    @Test
    fun unavailableResponseDoesNotReplayAWrite() {
        val requests = AtomicInteger()
        val command = PendingCommand(UUID.randomUUID().toString(), "test-plan", "a".repeat(64), "test only")
        withGateway("/operations", { exchange ->
            requests.incrementAndGet()
            exchange.requestBody.use { it.readAllBytes() }
            exchange.reply(503, """{"code":"UNAVAILABLE"}""")
        }) { client ->
            val error = assertThrows(ApiFailure::class.java) { client.submit(command) }
            assertEquals(503, error.status)
            assertEquals(1, requests.get())
        }
    }

    @Test
    fun commandRecoveryPreservesTheSameBodyAndRejectsAnExpiredPlan() {
        val expired = Instant.now().minusSeconds(2).toString()
        val command = PendingCommand(UUID.randomUUID().toString(), "test-plan", "a".repeat(64), "test only", expired)
        val restored = requireNotNull(PendingCommand.parse(command.storedJson().toString()))
        assertEquals(command, restored)
        assertEquals(command.json().toString(), restored.json().toString())
        assertFalse(restored.isPlanCurrent())
        assertFalse(restored.json().has("expires_at"))
    }

    private fun withGateway(path: String, reply: (HttpExchange) -> Unit, test: (MobileApi) -> Unit) {
        val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        server.createContext("/mobile/v1$path") { reply(it) }
        server.start()
        try {
            val profile = ConnectionProfile("http://127.0.0.1:${server.address.port}", "", "")
            test(MobileApi(profile, "fixture"))
        } finally {
            server.stop(0)
        }
    }

    private fun HttpExchange.reply(status: Int, json: String) {
        val bytes = json.encodeToByteArray()
        responseHeaders.add("Content-Type", "application/json")
        sendResponseHeaders(status, bytes.size.toLong())
        responseBody.use { it.write(bytes) }
    }
}
