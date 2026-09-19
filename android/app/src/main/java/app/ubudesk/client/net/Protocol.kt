package app.ubudesk.client.net

import org.json.JSONObject
import java.io.DataInputStream
import java.io.EOFException
import java.io.InputStream
import java.io.OutputStream
import java.nio.ByteBuffer
import java.nio.charset.StandardCharsets

/**
 * UbuDesk wire protocol v1 (see docs/PROTOCOL.md).
 *
 * Framing: | type: u8 | length: u32 BE | payload |
 *  - 0x01 CONTROL: UTF-8 JSON with a "t" field.
 *  - 0x02 VIDEO:   | pts_us: u64 BE | flags: u8 | H.264 Annex-B access unit |
 */
object Protocol {
    const val VERSION = 1
    const val TYPE_CONTROL = 0x01
    const val TYPE_VIDEO = 0x02
    const val MAX_PAYLOAD = 8 * 1024 * 1024
    const val VIDEO_HEADER_SIZE = 9

    const val FLAG_KEYFRAME = 0x01
    const val FLAG_CONFIG = 0x02

    class ProtocolException(message: String) : Exception(message)

    sealed interface Frame
    data class Control(val msg: JSONObject) : Frame {
        val t: String get() = msg.optString("t")
    }
    data class Video(val ptsUs: Long, val flags: Int, val data: ByteArray) : Frame {
        val isKeyframe: Boolean get() = flags and FLAG_KEYFRAME != 0
        val hasConfig: Boolean get() = flags and FLAG_CONFIG != 0

        override fun equals(other: Any?): Boolean =
            other is Video && ptsUs == other.ptsUs && flags == other.flags &&
                data.contentEquals(other.data)

        override fun hashCode(): Int = 31 * ptsUs.hashCode() + flags
    }

    fun encodeControl(msg: JSONObject): ByteArray {
        val payload = msg.toString().toByteArray(StandardCharsets.UTF_8)
        require(payload.size <= MAX_PAYLOAD) { "control message too large" }
        val buf = ByteBuffer.allocate(5 + payload.size)
        buf.put(TYPE_CONTROL.toByte())
        buf.putInt(payload.size)
        buf.put(payload)
        return buf.array()
    }

    /** Parses a complete frame from [wire]; used by tests and readFrame. */
    fun decodeFrame(wire: ByteArray): Frame {
        if (wire.size < 5) throw ProtocolException("truncated header")
        val buf = ByteBuffer.wrap(wire)
        val type = buf.get().toInt() and 0xFF
        val length = buf.int
        if (length > MAX_PAYLOAD) throw ProtocolException("payload exceeds 8 MiB")
        if (wire.size - 5 < length) throw ProtocolException("truncated payload")
        val payload = ByteArray(length)
        buf.get(payload)
        return decodePayload(type, payload)
    }

    fun decodePayload(type: Int, payload: ByteArray): Frame = when (type) {
        TYPE_CONTROL -> {
            val text = String(payload, StandardCharsets.UTF_8)
            val json = try {
                JSONObject(text)
            } catch (e: Exception) {
                throw ProtocolException("bad control JSON: ${e.message}")
            }
            if (!json.has("t")) throw ProtocolException("control message missing 't'")
            Control(json)
        }
        TYPE_VIDEO -> {
            if (payload.size < VIDEO_HEADER_SIZE) throw ProtocolException("video payload too short")
            val buf = ByteBuffer.wrap(payload)
            val pts = buf.long
            val flags = buf.get().toInt() and 0xFF
            val data = ByteArray(payload.size - VIDEO_HEADER_SIZE)
            buf.get(data)
            Video(pts, flags, data)
        }
        else -> throw ProtocolException("unknown frame type $type")
    }

    /** Blocking read of one frame from a stream. Returns null on unknown types (skipped). */
    fun readFrame(input: DataInputStream): Frame? {
        val type = try {
            input.readUnsignedByte()
        } catch (e: EOFException) {
            throw e
        }
        val length = input.readInt()
        if (length < 0 || length > MAX_PAYLOAD) throw ProtocolException("bad frame length $length")
        val payload = ByteArray(length)
        input.readFully(payload)
        return when (type) {
            TYPE_CONTROL, TYPE_VIDEO -> decodePayload(type, payload)
            else -> null // unknown type: ignore per spec
        }
    }

    fun writeControl(output: OutputStream, msg: JSONObject) {
        output.write(encodeControl(msg))
        output.flush()
    }

    // ---- message builders --------------------------------------------------

    fun hello(clientId: String, name: String, appVersion: String, w: Int, h: Int, dpi: Int, refresh: Int): JSONObject =
        JSONObject()
            .put("t", "hello")
            .put("proto", VERSION)
            .put("client_id", clientId)
            .put("name", name)
            .put("app_version", appVersion)
            .put(
                "screen",
                JSONObject().put("w", w).put("h", h).put("dpi", dpi).put("refresh", refresh),
            )
            .put("codecs", org.json.JSONArray().put("h264"))

    fun authPin(pin: String): JSONObject =
        JSONObject().put("t", "auth").put("method", "pin").put("pin", pin)

    fun authToken(token: String): JSONObject =
        JSONObject().put("t", "auth").put("method", "token").put("token", token)

    fun start(width: Int, height: Int, fps: Int, bitrateKbps: Int, mode: String, touchMode: String): JSONObject =
        JSONObject()
            .put("t", "start")
            .put("width", width and 1.inv())
            .put("height", height and 1.inv())
            .put("fps", fps)
            .put("bitrate_kbps", bitrateKbps)
            .put("mode", mode)
            .put("touch_mode", touchMode)

    fun touch(action: String, id: Int, x: Double, y: Double): JSONObject =
        JSONObject().put("t", "touch").put("a", action).put("id", id).put("x", x).put("y", y)

    fun mouse(action: String, button: String?, x: Double, y: Double): JSONObject {
        val o = JSONObject().put("t", "mouse").put("a", action).put("x", x).put("y", y)
        if (button != null) o.put("b", button)
        return o
    }

    fun scroll(dx: Double, dy: Double): JSONObject =
        JSONObject().put("t", "scroll").put("dx", dx).put("dy", dy)

    fun key(code: Int, down: Boolean): JSONObject =
        JSONObject().put("t", "key").put("code", code).put("down", down)

    fun text(s: String): JSONObject = JSONObject().put("t", "text").put("s", s)

    fun ping(seq: Int, ts: Long): JSONObject =
        JSONObject().put("t", "ping").put("seq", seq).put("ts", ts)

    fun idr(): JSONObject = JSONObject().put("t", "idr")

    fun stats(fps: Double, decodeMs: Double, dropped: Int): JSONObject =
        JSONObject().put("t", "stats").put("fps", fps).put("decode_ms", decodeMs).put("dropped", dropped)

    fun bye(): JSONObject = JSONObject().put("t", "bye")
}
