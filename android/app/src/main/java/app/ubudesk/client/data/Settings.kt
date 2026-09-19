package app.ubudesk.client.data

import android.content.Context
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.intPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map
import java.util.UUID

private val Context.dataStore by preferencesDataStore(name = "ubudesk")

class AppStorage(private val context: Context) {

    private object Keys {
        val CLIENT_ID = stringPreferencesKey("client_id")
        val SERVERS = stringPreferencesKey("servers_json")
        val RESOLUTION = stringPreferencesKey("resolution")
        val FPS = intPreferencesKey("fps")
        val BITRATE = intPreferencesKey("bitrate_kbps")
        val TOUCH_MODE = booleanPreferencesKey("touch_mode")
        val SHOW_STATS = booleanPreferencesKey("show_stats")
        val COMPAT_DECODE = booleanPreferencesKey("compat_decode")
        val MODE = stringPreferencesKey("mode")
    }

    suspend fun clientId(): String {
        val existing = context.dataStore.data.first()[Keys.CLIENT_ID]
        if (existing != null) return existing
        val id = UUID.randomUUID().toString()
        context.dataStore.edit { it[Keys.CLIENT_ID] = id }
        return id
    }

    val settings: Flow<StreamSettings> = context.dataStore.data.map { p ->
        StreamSettings(
            resolutionPreset = p[Keys.RESOLUTION] ?: "1920x1200",
            fps = p[Keys.FPS] ?: 60,
            bitrateKbps = p[Keys.BITRATE] ?: 15000,
            touchMode = p[Keys.TOUCH_MODE] ?: true,
            showStats = p[Keys.SHOW_STATS] ?: false,
            compatibilityDecode = p[Keys.COMPAT_DECODE] ?: false,
            mode = p[Keys.MODE] ?: "extend",
        )
    }

    suspend fun saveSettings(s: StreamSettings) {
        context.dataStore.edit { p ->
            p[Keys.RESOLUTION] = s.resolutionPreset
            p[Keys.FPS] = s.fps
            p[Keys.BITRATE] = s.bitrateKbps
            p[Keys.TOUCH_MODE] = s.touchMode
            p[Keys.SHOW_STATS] = s.showStats
            p[Keys.COMPAT_DECODE] = s.compatibilityDecode
            p[Keys.MODE] = s.mode
        }
    }

    val servers: Flow<List<KnownServer>> = context.dataStore.data.map { p ->
        ServerCodec.parse(p[Keys.SERVERS] ?: "[]")
    }

    suspend fun upsertServer(server: KnownServer) {
        context.dataStore.edit { p ->
            val list = ServerCodec.parse(p[Keys.SERVERS] ?: "[]")
                .filter { it.key != server.key } + server
            p[Keys.SERVERS] = ServerCodec.serialize(list)
        }
    }

    suspend fun removeServer(key: String) {
        context.dataStore.edit { p ->
            val list = ServerCodec.parse(p[Keys.SERVERS] ?: "[]").filter { it.key != key }
            p[Keys.SERVERS] = ServerCodec.serialize(list)
        }
    }
}
