package app.ubudesk.client.data

import org.json.JSONArray
import org.json.JSONObject

/**
 * Pure (non-Android) models and codecs, JVM-unit-testable without a device.
 */

/** Stream / input settings selected in the Settings screen. */
data class StreamSettings(
    val resolutionPreset: String = "1920x1200", // "native" or "WxH"
    val fps: Int = 60,
    val bitrateKbps: Int = 15000,
    val touchMode: Boolean = true,
    val showStats: Boolean = false,
    val compatibilityDecode: Boolean = false,
    val mode: String = "extend",
) {
    /** Resolve the preset into even (w, h) given the device's display size. */
    fun resolve(deviceW: Int, deviceH: Int): Pair<Int, Int> {
        val (w, h) = if (resolutionPreset == "native") {
            deviceW to deviceH
        } else {
            val parts = resolutionPreset.split("x")
            val pw = parts.getOrNull(0)?.toIntOrNull() ?: 1920
            val ph = parts.getOrNull(1)?.toIntOrNull() ?: 1200
            // aspect-match to the device: keep preset width, device aspect
            val aspect = if (deviceH > 0) deviceW.toDouble() / deviceH else pw.toDouble() / ph
            if (aspect >= 1.0) pw to (pw / aspect).toInt() else (ph * aspect).toInt() to ph
        }
        return (w and 1.inv()) to (h and 1.inv())
    }
}

/** A remembered (paired or manually added) server. */
data class KnownServer(
    val host: String,
    val port: Int,
    val name: String,
    val token: String?,
    val fingerprint: String?,
) {
    val key: String get() = "$host:$port"
}

/** JSON (de)serialization of the known-server list. */
object ServerCodec {
    fun parse(json: String): List<KnownServer> = try {
        val arr = JSONArray(json)
        (0 until arr.length()).map { i ->
            val o = arr.getJSONObject(i)
            KnownServer(
                host = o.getString("host"),
                port = o.optInt("port", 7777),
                name = o.optString("name", o.getString("host")),
                token = o.optString("token").ifEmpty { null },
                fingerprint = o.optString("fingerprint").ifEmpty { null },
            )
        }
    } catch (_: Exception) {
        emptyList()
    }

    fun serialize(list: List<KnownServer>): String {
        val arr = JSONArray()
        for (s in list) {
            arr.put(
                JSONObject()
                    .put("host", s.host)
                    .put("port", s.port)
                    .put("name", s.name)
                    .put("token", s.token ?: "")
                    .put("fingerprint", s.fingerprint ?: ""),
            )
        }
        return arr.toString()
    }
}
