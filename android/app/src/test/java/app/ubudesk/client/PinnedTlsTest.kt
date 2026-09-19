package app.ubudesk.client

import app.ubudesk.client.net.PinnedTrustManager
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class PinnedTlsTest {

    @Test
    fun constantTimeEqualsBasics() {
        assertTrue(PinnedTrustManager.constantTimeEquals("abcd", "abcd"))
        assertFalse(PinnedTrustManager.constantTimeEquals("abcd", "abce"))
        assertFalse(PinnedTrustManager.constantTimeEquals("abcd", "abc"))
        assertFalse(PinnedTrustManager.constantTimeEquals("", "a"))
        assertTrue(PinnedTrustManager.constantTimeEquals("", ""))
    }

    @Test
    fun shortCodeIsFirstEightUppercased() {
        assertEquals("DEADBEEF", PinnedTrustManager.shortCode("deadbeef00112233"))
    }
}
