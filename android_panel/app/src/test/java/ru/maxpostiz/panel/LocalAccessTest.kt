package ru.maxpostiz.panel

import org.junit.Assert.*
import org.junit.Test

class LocalAccessTest {
    @Test fun requiresFourAsciiDigits() {
        assertTrue(validPin("0123"))
        assertFalse(validPin("123"))
        assertFalse(validPin("12345"))
        assertFalse(validPin("１２３４"))
        assertFalse(validPin("12 3"))
    }

    @Test fun fifthAndSubsequentFailuresRequireAWait() {
        var attempts = PinAttempts()
        repeat(4) { attempts = attempts.failed(1_000) }
        assertEquals(0L, attempts.retryAt)
        attempts = attempts.failed(2_000)
        assertEquals(32_000L, attempts.retryAt)
        assertEquals(PinAttempts(5, 62_000), attempts.failed(32_000))
    }
}
