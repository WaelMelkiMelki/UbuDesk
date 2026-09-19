package app.ubudesk.client

import app.ubudesk.client.data.ServerCodec
import app.ubudesk.client.data.KnownServer
import app.ubudesk.client.data.StreamSettings
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class SettingsTest {

    @Test
    fun presetResolvesToDeviceAspectEvenNumbers() {
        val s = StreamSettings(resolutionPreset = "1920x1200")
        // 16:10 tablet
        val (w1, h1) = s.resolve(2560, 1600)
        assertEquals(1920, w1)
        assertEquals(1200, h1)
        // 16:9 phone -> width kept, height derived from device aspect
        val (w2, h2) = s.resolve(2400, 1080)
        assertEquals(1920, w2)
        assertEquals(864, h2)
        assertEquals(0, w2 % 2)
        assertEquals(0, h2 % 2)
    }

    @Test
    fun autoPresetPicksWidthByDeviceClass() {
        val s = StreamSettings(resolutionPreset = "auto")
        // 10" 16:10 tablet, 2560x1600 @ 320dpi -> sw = 1600*160/320 = 800dp >= 600 -> 1920 wide
        val (tw, th) = s.resolve(2560, 1600, 320)
        assertEquals(1920, tw)
        assertEquals(1200, th)
        // 6.7" phone, 2400x1080 @ 480dpi -> sw = 1080*160/480 = 360dp < 600 -> 1280 wide
        val (pw, ph) = s.resolve(2400, 1080, 480)
        assertEquals(1280, pw)
        assertEquals(576, ph) // device aspect 20:9 kept
        assertEquals(0, pw % 2)
        assertEquals(0, ph % 2)
    }

    @Test
    fun autoPreferredWidthThreshold() {
        // exactly 600dp smallest-width counts as tablet
        assertEquals(1920, StreamSettings.autoPreferredWidth(1920, 1200, 320))
        assertEquals(1280, StreamSettings.autoPreferredWidth(2340, 1080, 440))
    }

    @Test
    fun nativeResolvesToDeviceSize() {
        val s = StreamSettings(resolutionPreset = "native")
        val (w, h) = s.resolve(2561, 1601) // odd device sizes get evened
        assertEquals(2560, w)
        assertEquals(1600, h)
    }

    @Test
    fun startMessageFromSettings() {
        val s = StreamSettings(resolutionPreset = "1600x1000", fps = 30, bitrateKbps = 8000, mode = "mirror")
        val (w, h) = s.resolve(2560, 1600)
        val msg = app.ubudesk.client.net.Protocol.start(w, h, s.fps, s.bitrateKbps, s.mode, "touch")
        assertEquals("start", msg.getString("t"))
        assertEquals(1600, msg.getInt("width"))
        assertEquals(1000, msg.getInt("height"))
        assertEquals(30, msg.getInt("fps"))
        assertEquals(8000, msg.getInt("bitrate_kbps"))
        assertEquals("mirror", msg.getString("mode"))
    }

    @Test
    fun serverListRoundTrip() {
        val servers = listOf(
            KnownServer("192.168.1.10", 7777, "my-pc", "tok123", "aa".repeat(32)),
            KnownServer("127.0.0.1", 7777, "USB", null, null),
        )
        val json = ServerCodec.serialize(servers)
        val parsed = ServerCodec.parse(json)
        assertEquals(2, parsed.size)
        assertEquals("my-pc", parsed[0].name)
        assertEquals("tok123", parsed[0].token)
        assertNull(parsed[1].token)
        assertNull(parsed[1].fingerprint)
    }

    @Test
    fun corruptServerJsonYieldsEmptyList() {
        assertEquals(0, ServerCodec.parse("{not json").size)
    }
}
