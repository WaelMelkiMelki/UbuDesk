package app.ubudesk.client

import app.ubudesk.client.input.TouchMapper
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Test

class TouchMapperTest {

    @Test
    fun exactFitNoLetterbox() {
        val rect = TouchMapper.videoRect(1920f, 1200f, 1920f, 1200f)
        assertEquals(0f, rect.left, 0.01f)
        assertEquals(0f, rect.top, 0.01f)
        assertEquals(1920f, rect.width, 0.01f)
        assertEquals(1200f, rect.height, 0.01f)
    }

    @Test
    fun widerVideoGetsTopBottomBars() {
        // 21:9 video in a 16:10 view
        val rect = TouchMapper.videoRect(1600f, 1000f, 2100f, 900f)
        assertEquals(0f, rect.left, 0.01f)
        assertEquals(1600f, rect.width, 0.01f)
        val expectedH = 1600f / (2100f / 900f)
        assertEquals(expectedH, rect.height, 0.5f)
        assertEquals((1000f - expectedH) / 2f, rect.top, 0.5f)
    }

    @Test
    fun tallerVideoGetsSideBars() {
        // 4:3 video in a 16:10 view
        val rect = TouchMapper.videoRect(1600f, 1000f, 1200f, 900f)
        assertEquals(0f, rect.top, 0.01f)
        assertEquals(1000f, rect.height, 0.01f)
        val expectedW = 1000f * (1200f / 900f)
        assertEquals(expectedW, rect.width, 0.5f)
        assertEquals((1600f - expectedW) / 2f, rect.left, 0.5f)
    }

    @Test
    fun normalizeCenterIsHalf() {
        val rect = TouchMapper.videoRect(1920f, 1200f, 1920f, 1200f)
        val n = TouchMapper.normalize(rect, 960f, 600f)
        assertNotNull(n)
        assertEquals(0.5, n!!.first, 1e-3)
        assertEquals(0.5, n.second, 1e-3)
    }

    @Test
    fun pointInLetterboxBarIsRejected() {
        // wide video: bars top and bottom
        val rect = TouchMapper.videoRect(1600f, 1000f, 3200f, 1000f) // video aspect 3.2
        // top bar (y above rect.top)
        assertNull(TouchMapper.normalize(rect, 800f, 10f))
        // inside
        assertNotNull(TouchMapper.normalize(rect, 800f, 500f))
    }

    @Test
    fun normalizeCornersMapTo01() {
        val rect = TouchMapper.videoRect(1600f, 1000f, 1600f, 1000f)
        val tl = TouchMapper.normalize(rect, 0f, 0f)!!
        assertEquals(0.0, tl.first, 1e-6)
        assertEquals(0.0, tl.second, 1e-6)
        val br = TouchMapper.normalize(rect, 1600f, 1000f)!!
        assertEquals(1.0, br.first, 1e-6)
        assertEquals(1.0, br.second, 1e-6)
    }

    @Test
    fun clampedNeverEscapesUnitRange() {
        val rect = TouchMapper.videoRect(1600f, 1000f, 1200f, 900f)
        val n = TouchMapper.normalizeClamped(rect, -500f, 5000f)
        assertEquals(0.0, n.first, 1e-6)
        assertEquals(1.0, n.second, 1e-6)
    }

    @Test
    fun degenerateViewDoesNotCrash() {
        val rect = TouchMapper.videoRect(0f, 0f, 1920f, 1200f)
        assertNull(TouchMapper.normalize(rect, 10f, 10f))
    }
}
