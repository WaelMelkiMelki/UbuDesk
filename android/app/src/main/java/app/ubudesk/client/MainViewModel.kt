package app.ubudesk.client

import android.app.Application
import android.util.Log
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import app.ubudesk.client.data.AppStorage
import app.ubudesk.client.data.KnownServer
import app.ubudesk.client.data.StreamSettings
import app.ubudesk.client.discovery.ServerDiscovery
import app.ubudesk.client.net.Protocol
import app.ubudesk.client.net.UbuDeskClient
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch

sealed interface UiState {
    data object Connect : UiState
    data class PairingPin(val server: KnownServer, val fingerprintShort: String?) : UiState
    data class Connecting(val server: KnownServer, val status: String) : UiState
    data class Streaming(
        val server: KnownServer,
        val width: Int,
        val height: Int,
        val fps: Int,
        val encoder: String,
    ) : UiState

    data class Failed(val message: String) : UiState
}

class MainViewModel(app: Application) : AndroidViewModel(app) {

    companion object {
        private const val TAG = "UbuDesk"
    }

    val storage = AppStorage(app)
    private val discovery = ServerDiscovery(app)

    private val _state = MutableStateFlow<UiState>(UiState.Connect)
    val state: StateFlow<UiState> = _state

    private val _discovered = MutableStateFlow<List<ServerDiscovery.DiscoveredServer>>(emptyList())
    val discovered: StateFlow<List<ServerDiscovery.DiscoveredServer>> = _discovered

    val knownServers = storage.servers.stateIn(
        viewModelScope, SharingStarted.Eagerly, emptyList(),
    )
    val settings = storage.settings.stateIn(
        viewModelScope, SharingStarted.Eagerly, StreamSettings(),
    )

    private val _stats = MutableStateFlow("")
    val stats: StateFlow<String> = _stats

    var client: UbuDeskClient? = null
        private set

    /** Callbacks the stream screen wires up. */
    var onVideoFrame: ((Protocol.Video) -> Unit)? = null

    private var pendingServer: KnownServer? = null
    private var pendingPin: String? = null
    private var capturedFingerprint: String? = null
    private var deviceW = 1920
    private var deviceH = 1200
    private var deviceDpi = 240
    private var deviceRefresh = 60

    fun setDeviceMetrics(w: Int, h: Int, dpi: Int, refresh: Int) {
        deviceW = w
        deviceH = h
        deviceDpi = dpi
        deviceRefresh = refresh
    }

    fun startDiscovery() {
        discovery.start(
            onFound = { server ->
                _discovered.value =
                    _discovered.value.filter { it.name != server.name } + server
            },
            onLost = { name ->
                _discovered.value = _discovered.value.filter { it.name != name }
            },
        )
    }

    fun stopDiscovery() = discovery.stop()

    fun connectTo(server: KnownServer, pin: String? = null) {
        pendingServer = server
        pendingPin = pin
        capturedFingerprint = null
        _state.value = UiState.Connecting(server, "connecting to ${server.host}:${server.port}…")

        val useTls = true
        val c = UbuDeskClient(
            host = server.host,
            port = server.port,
            useTls = useTls,
            pinnedFingerprint = server.fingerprint,
            listener = listener,
        )
        client = c
        c.connect()
        viewModelScope.launch {
            val id = storage.clientId()
            c.sendHello(
                id,
                android.os.Build.MODEL ?: "Android",
                BuildConfig.VERSION_NAME,
                deviceW, deviceH, deviceDpi, deviceRefresh,
            )
        }
    }

    fun submitPin(pin: String) {
        val server = pendingServer ?: return
        pendingPin = pin
        _state.value = UiState.Connecting(server, "pairing…")
        client?.send(Protocol.authPin(pin))
    }

    fun disconnect() {
        client?.disconnect()
        client = null
        _state.value = UiState.Connect
    }

    fun requestIdr() {
        client?.send(Protocol.idr())
    }

    fun sendStats(fps: Double, decodeMs: Double, dropped: Int) {
        client?.send(Protocol.stats(fps, decodeMs, dropped))
        _stats.value = "%.0f fps  %.1f ms  %d dropped".format(fps, decodeMs, dropped)
    }

    fun saveSettings(s: StreamSettings) {
        viewModelScope.launch { storage.saveSettings(s) }
    }

    fun forgetServer(key: String) {
        viewModelScope.launch { storage.removeServer(key) }
    }

    private val listener = object : UbuDeskClient.Listener {
        override fun onFingerprintCaptured(fingerprintHex: String) {
            capturedFingerprint = fingerprintHex
        }

        override fun onAuthRequired(serverName: String, methods: List<String>) {
            val server = pendingServer ?: return
            val token = server.token
            val pin = pendingPin
            when {
                token != null -> client?.send(Protocol.authToken(token))
                pin != null -> client?.send(Protocol.authPin(pin))
                else -> _state.value = UiState.PairingPin(
                    server,
                    capturedFingerprint?.let { it.take(8).uppercase() },
                )
            }
        }

        override fun onAuthOk(newToken: String?) {
            val server = pendingServer ?: return
            val s = settings.value
            if (newToken != null) {
                val updated = server.copy(
                    token = newToken,
                    fingerprint = capturedFingerprint ?: server.fingerprint,
                )
                pendingServer = updated
                viewModelScope.launch { storage.upsertServer(updated) }
            }
            val (w, h) = s.resolve(deviceW, deviceH)
            _state.value = UiState.Connecting(server, "starting stream…")
            client?.send(Protocol.start(w, h, s.fps, s.bitrateKbps, s.mode, if (s.touchMode) "touch" else "mouse"))
        }

        override fun onAuthFail(reason: String, retryAfterS: Int) {
            val server = pendingServer
            if (reason == "unknown_token" && server != null) {
                // stale token: forget it and re-pair
                val cleared = server.copy(token = null)
                pendingServer = cleared
                viewModelScope.launch { storage.upsertServer(cleared) }
                _state.value = UiState.Failed("Saved pairing was revoked on the PC. Reconnect to pair again.")
                return
            }
            val hint = when (reason) {
                "bad_pin" -> "Wrong PIN."
                "pin_expired" -> "PIN expired. Run `ubudesk serve --pair` on the PC for a new one."
                "locked" -> "Too many attempts; locked for ${retryAfterS}s."
                else -> "Authentication failed ($reason)."
            }
            _state.value = UiState.Failed(hint)
        }

        override fun onStarted(width: Int, height: Int, fps: Int, encoder: String) {
            val server = pendingServer ?: return
            Log.i(TAG, "started $width x $height @$fps via $encoder")
            _state.value = UiState.Streaming(server, width, height, fps, encoder)
        }

        override fun onVideo(frame: Protocol.Video) {
            onVideoFrame?.invoke(frame)
        }

        override fun onServerError(code: String, message: String) {
            val hint = when (code) {
                "no_virtual_monitor" ->
                    "This PC's desktop portal doesn't support virtual monitors. " +
                        "Switch mode to 'mirror' in Settings, or see docs/TROUBLESHOOTING.md."
                "capture_failed" ->
                    "Screen capture failed on the PC: $message"
                else -> "$code: $message"
            }
            _state.value = UiState.Failed(hint)
        }

        override fun onDisconnected(reason: String) {
            if (_state.value !is UiState.Connect && _state.value !is UiState.Failed) {
                _state.value = UiState.Failed("Disconnected: $reason")
            }
            client = null
        }
    }

    override fun onCleared() {
        discovery.stop()
        client?.disconnect()
    }
}
