package app.ubudesk.client.net

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.io.BufferedOutputStream
import java.io.DataInputStream
import java.net.InetSocketAddress
import java.net.Socket
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong
import java.util.logging.Level
import java.util.logging.Logger
import javax.net.ssl.SSLSocket

/**
 * TCP/TLS connection with a dedicated reader thread and a serialized writer.
 *
 * Before auth_ok the server accepts only hello/auth, so heartbeats MUST NOT run
 * while the user compares the security code and enters a PIN. Pairing has its
 * own bounded deadline; the normal six-second idle rule resumes after auth_ok,
 * including while the desktop capture permission dialog is open.
 *
 * This transport has no Android framework dependencies, so its real socket
 * lifecycle and timing can be covered by JVM tests without a phone/emulator.
 */
class UbuDeskClient(
    private val host: String,
    private val port: Int,
    private val useTls: Boolean,
    private val pinnedFingerprint: String?,
    private val listener: Listener,
    private val timeouts: Timeouts = Timeouts(),
) {
    interface Listener {
        fun onAuthRequired(serverName: String, methods: List<String>)
        fun onAuthOk(newToken: String?)
        fun onAuthFail(reason: String, retryAfterS: Int)
        fun onStarted(width: Int, height: Int, fps: Int, encoder: String)
        fun onVideo(frame: Protocol.Video)
        fun onServerError(code: String, message: String)
        fun onDisconnected(reason: String)
        /** Fingerprint seen during the TLS handshake (for TOFU pinning). */
        fun onFingerprintCaptured(fingerprintHex: String) {}
    }

    /** Defaults mirror docs/PROTOCOL.md; injectable for fast socket regressions. */
    data class Timeouts(
        val connectMs: Int = 6000,
        val idleMs: Int = 6000,
        val pairingMs: Int = 120_000,
        val pingIntervalMs: Long = 2000,
    ) {
        init {
            require(connectMs > 0 && idleMs > 0 && pairingMs > 0)
            require(pingIntervalMs > 0 && pingIntervalMs < idleMs)
        }
    }

    private enum class Phase { HANDSHAKE, PAIRING, AUTHENTICATED }

    companion object {
        private val log = Logger.getLogger("UbuDesk")
    }

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val outgoing = Channel<JSONObject>(capacity = 256)
    private val lifecycleLock = Any()
    private var socket: Socket? = null // guarded by lifecycleLock
    private val connecting = AtomicBoolean(false)
    @Volatile private var closed = false
    @Volatile private var phase = Phase.HANDSHAKE
    private var pingJob: Job? = null
    private val lastReceivedNanos = AtomicLong(0)

    fun connect() {
        if (closed || !connecting.compareAndSet(false, true)) return
        scope.launch {
            try {
                openSocketAndRun()
            } catch (e: Exception) {
                if (!closed) log.log(Level.WARNING, "connection failed", e)
                disconnect(e.message ?: "connection failed")
            }
        }
    }

    /** Publish before connecting/handshaking, so cancellation can close it too. */
    private fun registerSocket(candidate: Socket): Boolean = synchronized(lifecycleLock) {
        if (closed) {
            runCatching { candidate.close() }
            false
        } else {
            socket = candidate
            true
        }
    }

    private fun openSocketAndRun() {
        val raw = Socket()
        if (!registerSocket(raw)) return
        raw.tcpNoDelay = true
        raw.connect(InetSocketAddress(host, port), timeouts.connectMs)
        raw.soTimeout = timeouts.idleMs

        val sock: Socket = if (useTls) {
            val tm = PinnedTrustManager(pinnedFingerprint)
            val factory = PinnedTrustManager.socketFactory(tm)
            val tls = factory.createSocket(raw, host, port, true) as SSLSocket
            if (!registerSocket(tls)) return
            tls.soTimeout = timeouts.idleMs
            tls.startHandshake()
            if (closed) return
            tm.capturedFingerprint?.let { listener.onFingerprintCaptured(it) }
            tls
        } else {
            raw
        }
        if (closed) return
        lastReceivedNanos.set(System.nanoTime())
        val output = BufferedOutputStream(sock.getOutputStream(), 64 * 1024)

        scope.launch {
            try {
                for (msg in outgoing) {
                    if (closed) break
                    Protocol.writeControl(output, msg)
                }
            } catch (e: Exception) {
                disconnect(e.message ?: "writer failed")
            }
        }

        Thread({
            try {
                val input = DataInputStream(sock.getInputStream().buffered(256 * 1024))
                while (!closed) {
                    val frame = Protocol.readFrame(input) ?: continue
                    lastReceivedNanos.set(System.nanoTime())
                    if (closed) break
                    when (frame) {
                        is Protocol.Video -> {
                            if (phase != Phase.AUTHENTICATED) {
                                throw Protocol.ProtocolException("video before authentication")
                            }
                            listener.onVideo(frame)
                        }
                        is Protocol.Control -> handleControl(frame.msg, sock)
                    }
                }
            } catch (e: Exception) {
                if (!closed) log.fine("reader ended: ${e.message}")
                disconnect(e.message ?: "connection lost")
            }
        }, "ubudesk-reader").start()
    }

    private fun startHeartbeat() {
        if (closed || pingJob != null) return
        pingJob = scope.launch {
            var seq = 0
            while (isActive && !closed) {
                delay(timeouts.pingIntervalMs)
                val silenceMs = (System.nanoTime() - lastReceivedNanos.get()) / 1_000_000
                if (silenceMs >= timeouts.idleMs) {
                    disconnect("server silent for ${silenceMs}ms")
                    break
                }
                send(Protocol.ping(++seq, System.currentTimeMillis()))
            }
        }
    }

    private fun handleControl(msg: JSONObject, sock: Socket) {
        when (msg.optString("t")) {
            "auth_required" -> {
                if (phase != Phase.HANDSHAKE) {
                    throw Protocol.ProtocolException("unexpected auth_required")
                }
                phase = Phase.PAIRING
                sock.soTimeout = timeouts.pairingMs
                val methods = mutableListOf<String>()
                val arr = msg.optJSONArray("methods")
                if (arr != null) for (i in 0 until arr.length()) methods.add(arr.optString(i))
                listener.onAuthRequired(msg.optString("server_name"), methods)
            }
            "auth_ok" -> {
                if (phase != Phase.PAIRING) {
                    throw Protocol.ProtocolException("unexpected auth_ok")
                }
                phase = Phase.AUTHENTICATED
                sock.soTimeout = timeouts.idleMs
                startHeartbeat()
                listener.onAuthOk(msg.optString("token").ifEmpty { null })
            }
            "auth_fail" -> {
                val reason = msg.optString("reason", "unknown")
                listener.onAuthFail(reason, msg.optInt("retry_after_s", 0))
                disconnect("authentication failed: $reason")
            }
            "started" -> listener.onStarted(
                msg.optInt("width"),
                msg.optInt("height"),
                msg.optInt("fps"),
                msg.optString("encoder"),
            )
            "error" -> listener.onServerError(
                msg.optString("code", "unknown"),
                msg.optString("message", ""),
            )
            "pong" -> Unit
            "bye" -> disconnect("server said bye")
            else -> Unit // unknown "t": ignore per spec
        }
    }

    fun send(msg: JSONObject) {
        if (closed) return
        if (phase != Phase.AUTHENTICATED && msg.optString("t") !in setOf("hello", "auth")) {
            // A decoder from a just-disposed screen can still report stats or
            // request an IDR. Never let those callbacks break a new handshake.
            return
        }
        if (outgoing.trySend(msg).isFailure) {
            // Never silently drop a key/button release or an auth message.
            disconnect("outgoing control queue is full")
        }
    }

    fun sendHello(clientId: String, deviceName: String, appVersion: String, w: Int, h: Int, dpi: Int, refresh: Int) =
        send(Protocol.hello(clientId, deviceName, appVersion, w, h, dpi, refresh))

    fun disconnect(reason: String = "user disconnect") {
        val toClose = synchronized(lifecycleLock) {
            if (closed) return
            closed = true
            socket.also { socket = null }
        }
        outgoing.close()
        pingJob?.cancel()
        // EOF is the authoritative disconnect signal. Do not keep a failing
        // connection alive just to flush a best-effort bye message.
        scope.launch {
            try {
                runCatching { toClose?.close() }
            } finally {
                scope.cancel()
            }
        }
        listener.onDisconnected(reason)
    }
}
