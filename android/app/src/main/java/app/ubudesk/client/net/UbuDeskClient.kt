package app.ubudesk.client.net

import android.util.Log
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
import java.io.DataInputStream
import java.io.BufferedOutputStream
import java.net.InetSocketAddress
import java.net.Socket
import java.util.concurrent.atomic.AtomicLong
import javax.net.ssl.SSLSocket

/**
 * Connection to a UbuDesk server: TCP( +TLS ), handshake, then a reader thread
 * for incoming frames and a writer coroutine for outgoing control messages.
 */
class UbuDeskClient(
    private val host: String,
    private val port: Int,
    private val useTls: Boolean,
    private val pinnedFingerprint: String?,
    private val listener: Listener,
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

    companion object {
        private const val TAG = "UbuDesk"
        private const val CONNECT_TIMEOUT_MS = 6000
        private const val IDLE_TIMEOUT_MS = 6000
        private const val PING_INTERVAL_MS = 2000L
    }

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val outgoing = Channel<JSONObject>(capacity = 256)
    private var socket: Socket? = null
    private var readerThread: Thread? = null
    private var pingJob: Job? = null
    private val lastReceivedAt = AtomicLong(0)
    @Volatile private var closed = false

    fun connect() {
        scope.launch {
            try {
                openSocketAndRun()
            } catch (e: Exception) {
                if (!closed) {
                    Log.w(TAG, "connection failed", e)
                    listener.onDisconnected(e.message ?: "connection failed")
                }
            }
        }
    }

    private fun openSocketAndRun() {
        val raw = Socket()
        raw.tcpNoDelay = true
        raw.connect(InetSocketAddress(host, port), CONNECT_TIMEOUT_MS)
        raw.soTimeout = IDLE_TIMEOUT_MS

        val sock: Socket = if (useTls) {
            val tm = PinnedTrustManager(pinnedFingerprint)
            val factory = PinnedTrustManager.socketFactory(tm)
            val tls = factory.createSocket(raw, host, port, true) as SSLSocket
            tls.soTimeout = IDLE_TIMEOUT_MS
            tls.startHandshake()
            tm.capturedFingerprint?.let { listener.onFingerprintCaptured(it) }
            tls
        } else {
            raw
        }
        socket = sock
        lastReceivedAt.set(System.currentTimeMillis())

        val output = BufferedOutputStream(sock.getOutputStream(), 64 * 1024)

        // writer coroutine
        scope.launch {
            try {
                for (msg in outgoing) {
                    Protocol.writeControl(output, msg)
                }
            } catch (e: Exception) {
                if (!closed) Log.w(TAG, "writer stopped: ${e.message}")
            }
        }

        // ping loop
        pingJob = scope.launch {
            var seq = 0
            while (isActive) {
                delay(PING_INTERVAL_MS)
                send(Protocol.ping(++seq, System.currentTimeMillis()))
                val silence = System.currentTimeMillis() - lastReceivedAt.get()
                if (silence > IDLE_TIMEOUT_MS) {
                    disconnect("server silent for ${silence}ms")
                    break
                }
            }
        }

        // reader loop on a dedicated thread (blocking I/O, video-rate hot path)
        readerThread = Thread({
            val input = DataInputStream(sock.getInputStream().buffered(256 * 1024))
            try {
                while (!closed) {
                    val frame = Protocol.readFrame(input) ?: continue
                    lastReceivedAt.set(System.currentTimeMillis())
                    when (frame) {
                        is Protocol.Video -> listener.onVideo(frame)
                        is Protocol.Control -> handleControl(frame.msg)
                    }
                }
            } catch (e: Exception) {
                if (!closed) {
                    Log.i(TAG, "reader ended: ${e.message}")
                    listener.onDisconnected(e.message ?: "connection lost")
                }
            }
        }, "ubudesk-reader")
        readerThread!!.start()
    }

    private fun handleControl(msg: JSONObject) {
        when (msg.optString("t")) {
            "auth_required" -> {
                val methods = mutableListOf<String>()
                val arr = msg.optJSONArray("methods")
                if (arr != null) for (i in 0 until arr.length()) methods.add(arr.optString(i))
                listener.onAuthRequired(msg.optString("server_name"), methods)
            }
            "auth_ok" -> listener.onAuthOk(msg.optString("token").ifEmpty { null })
            "auth_fail" -> listener.onAuthFail(
                msg.optString("reason", "unknown"),
                msg.optInt("retry_after_s", 0),
            )
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
        outgoing.trySend(msg)
    }

    fun sendHello(clientId: String, deviceName: String, appVersion: String, w: Int, h: Int, dpi: Int, refresh: Int) =
        send(Protocol.hello(clientId, deviceName, appVersion, w, h, dpi, refresh))

    fun disconnect(reason: String = "user disconnect") {
        if (closed) return
        closed = true
        runCatching { send(Protocol.bye()) }
        pingJob?.cancel()
        scope.launch {
            delay(150) // give bye a chance to flush
            runCatching { socket?.close() }
            outgoing.close()
            scope.cancel()
        }
        listener.onDisconnected(reason)
    }
}
