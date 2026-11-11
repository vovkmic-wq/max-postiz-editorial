package ru.maxpostiz.panel

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Test

class RememberedLoginTest {
    @Test fun firstRememberedLoginRequiresBothFields() {
        for ((name, password) in listOf("" to "", "owner" to "", " " to "example")) {
            assertThrows(IllegalArgumentException::class.java) { loginToRemember(name, password, null) }
        }
    }

    @Test fun blankReplacementKeepsThePasswordForTheSameAccountOnly() {
        assertNull(loginToRemember(" owner ", "", "owner"))
        assertThrows(IllegalArgumentException::class.java) { loginToRemember("other", "", "owner") }
    }

    @Test fun newPasswordIsSavedExactlyAsEntered() {
        assertEquals(SavedLogin("owner", " example "), loginToRemember(" owner ", " example ", "owner"))
    }
}
