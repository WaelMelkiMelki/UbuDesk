package app.ubudesk.client

import app.ubudesk.client.net.Protocol
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.File

/**
 * Runs the SAME golden vectors as the Python tests (protocol/vectors/),
 * keeping the two protocol implementations in lockstep.
 */
class ProtocolVectorTest {

    private val vectorDir: File by lazy {
        // module dir is android/app; vectors live at ../../protocol/vectors
        var dir = File(System.getProperty("user.dir") ?: ".")
        repeat(5) {
            val candidate = File(dir, "protocol/vectors")
            if (candidate.isDirectory) return@lazy candidate
            dir = dir.parentFile ?: return@lazy candidate
        }
        fail("protocol/vectors not found relative to ${System.getProperty("user.dir")}")
        throw IllegalStateException()
    }

    private fun jsonEquals(expected: Any?, actual: Any?, path: String) {
        when (expected) {
            is JSONObject -> {
                assertTrue("$path: expected object", actual is JSONObject)
                actual as JSONObject
                // keys() exists in both Android's JSONObject API and the JVM
                // test implementation; keySet() is specific to JSON-java.
                val expectedKeys = expected.keys().asSequence().toSet()
                val actualKeys = actual.keys().asSequence().toSet()
                assertEquals("$path: key sets differ", expectedKeys, actualKeys)
                for (key in expectedKeys) {
                    jsonEquals(expected.get(key), actual.get(key), "$path.$key")
                }
            }
            is org.json.JSONArray -> {
                assertTrue("$path: expected array", actual is org.json.JSONArray)
                actual as org.json.JSONArray
                assertEquals("$path: array length", expected.length(), actual.length())
                for (i in 0 until expected.length()) {
                    jsonEquals(expected.get(i), actual.get(i), "$path[$i]")
                }
            }
            is Number -> {
                assertTrue("$path: expected number, got $actual", actual is Number)
                assertEquals("$path: number", expected.toDouble(), (actual as Number).toDouble(), 1e-9)
            }
            else -> assertEquals("$path: value", expected, actual)
        }
    }

    @Test
    fun controlVectorsDecode() {
        val files = vectorDir.listFiles { f -> f.name.startsWith("control_") && f.name.endsWith(".json") }!!
        assertTrue("need at least 15 control vectors, got ${files.size}", files.size >= 15)
        for (jsonFile in files) {
            val expect = JSONObject(jsonFile.readText())
            val wire = File(jsonFile.path.removeSuffix(".json") + ".bin").readBytes()
            val frame = Protocol.decodeFrame(wire)
            assertTrue(jsonFile.name, frame is Protocol.Control)
            jsonEquals(expect.getJSONObject("message"), (frame as Protocol.Control).msg, jsonFile.name)
        }
    }

    @Test
    fun videoVectorsDecode() {
        val names = listOf("video_keyframe", "video_delta", "video_large_pts")
        for (name in names) {
            val expect = JSONObject(File(vectorDir, "$name.json").readText())
            val wire = File(vectorDir, "$name.bin").readBytes()
            val frame = Protocol.decodeFrame(wire)
            assertTrue(name, frame is Protocol.Video)
            frame as Protocol.Video
            assertEquals(name, expect.getLong("pts_us"), frame.ptsUs)
            assertEquals(name, expect.getInt("flags"), frame.flags)
            assertEquals(name, expect.getBoolean("keyframe"), frame.isKeyframe)
            assertEquals(name, expect.getBoolean("has_config"), frame.hasConfig)
            assertEquals(name, expect.getInt("data_len"), frame.data.size)
            val nalTypes = annexbNalTypes(frame.data)
            val expectedNals = expect.getJSONArray("nal_types")
            assertEquals(name, expectedNals.length(), nalTypes.size)
            for (i in nalTypes.indices) {
                assertEquals("$name nal $i", expectedNals.getInt(i), nalTypes[i])
            }
        }
    }

    @Test
    fun oversizedFrameRejected() {
        val wire = File(vectorDir, "bad_oversized.bin").readBytes()
        try {
            Protocol.decodeFrame(wire)
            fail("oversized frame must be rejected")
        } catch (_: Protocol.ProtocolException) {
        }
    }

    @Test
    fun truncatedFrameRejected() {
        val wire = File(vectorDir, "bad_truncated.bin").readBytes()
        try {
            Protocol.decodeFrame(wire)
            fail("truncated frame must be rejected")
        } catch (_: Protocol.ProtocolException) {
        }
    }

    @Test
    fun badJsonRejected() {
        val wire = File(vectorDir, "bad_json.bin").readBytes()
        try {
            Protocol.decodeFrame(wire)
            fail("bad JSON must be rejected")
        } catch (_: Protocol.ProtocolException) {
        }
    }

    @Test
    fun controlRoundTrip() {
        val msg = Protocol.start(1921, 1201, 60, 15000, "extend", "touch")
        // width/height must be rounded down to even
        assertEquals(1920, msg.getInt("width"))
        assertEquals(1200, msg.getInt("height"))
        val wire = Protocol.encodeControl(msg)
        val decoded = Protocol.decodeFrame(wire) as Protocol.Control
        assertEquals("start", decoded.t)
        assertEquals(msg.toString(), decoded.msg.toString())
    }

    private fun annexbNalTypes(data: ByteArray): List<Int> {
        val types = mutableListOf<Int>()
        var i = 0
        while (i + 3 < data.size) {
            if (data[i] == 0.toByte() && data[i + 1] == 0.toByte()) {
                if (data[i + 2] == 1.toByte()) {
                    types.add(data[i + 3].toInt() and 0x1F)
                    i += 4
                    continue
                }
                if (data[i + 2] == 0.toByte() && i + 4 < data.size && data[i + 3] == 1.toByte()) {
                    types.add(data[i + 4].toInt() and 0x1F)
                    i += 5
                    continue
                }
            }
            i++
        }
        return types
    }
}
