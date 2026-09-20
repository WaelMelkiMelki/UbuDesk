package app.ubudesk.client

import app.ubudesk.client.net.Protocol
import app.ubudesk.client.net.UbuDeskClient
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.DataInputStream
import java.net.InetAddress
import java.net.ServerSocket
import java.net.SocketTimeoutException
import java.util.concurrent.CountDownLatch
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

/** Real loopback sockets, not timing-policy mocks; no Android device required. */
class UbuDeskClientTest {
    private class Events : UbuDeskClient.Listener {
        val pairing = CountDownLatch(1)
        val authenticated = CountDownLatch(1)
        val started = CountDownLatch(1)
        val disconnected = LinkedBlockingQueue<String>()
        val disconnectCount = AtomicInteger(0)
        var authFailure: String? = null

        override fun onAuthRequired(serverName: String, methods: List<String>) { pairing.countDown() }
        override fun onAuthOk(newToken: String?) { authenticated.countDown() }
        override fun onAuthFail(reason: String, retryAfterS: Int) { authFailure = reason }
        override fun onStarted(width: Int, height: Int, fps: Int, encoder: String) { started.countDown() }
        override fun onVideo(frame: Protocol.Video) {}
        override fun onServerError(code: String, message: String) {}
        override fun onDisconnected(reason: String) {
            disconnectCount.incrementAndGet()
            disconnected.add(reason)
        }
    }

    private class Connection(
        val timeouts: UbuDeskClient.Timeouts = UbuDeskClient.Timeouts(
            connectMs = 2000, idleMs = 500, pairingMs = 2000, pingIntervalMs = 100,
        ),
        tls: Boolean = false,
    ) : AutoCloseable {
        private val server = ServerSocket(0, 1, InetAddress.getLoopbackAddress()).apply {
            soTimeout = 3000
        }
        val events = Events()
        val client = UbuDeskClient(
            host = server.inetAddress.hostAddress!!,
            port = server.localPort,
            useTls = tls,
            pinnedFingerprint = null,
            listener = events,
            timeouts = timeouts,
        )
        val peer = run {
            client.connect()
            client.sendHello("phone", "Phone", "test", 320, 200, 160, 30)
            server.accept().apply { soTimeout = 3000 }
        }
        val input = DataInputStream(peer.getInputStream())

        fun send(t: String, configure: (JSONObject) -> Unit = {}) {
            val msg = JSONObject().put("t", t)
            configure(msg)
            Protocol.writeControl(peer.getOutputStream(), msg)
        }

        fun readControl(): JSONObject = (Protocol.readFrame(input) as Protocol.Control).msg

        fun beginPairing() {
            assertEquals("hello", readControl().getString("t"))
            send("auth_required") { it.put("server_name", "PC") }
            assertTrue(events.pairing.await(1, TimeUnit.SECONDS))
        }

        fun authenticate() {
            beginPairing()
            client.send(Protocol.authPin("123456"))
            assertEquals("auth", readControl().getString("t"))
            send("auth_ok")
            assertTrue(events.authenticated.await(1, TimeUnit.SECONDS))
        }

        fun assertDisconnectedOnce() {
            assertTrue(events.disconnected.poll(3, TimeUnit.SECONDS) != null)
            client.disconnect() // explicit close racing an error must not notify twice
            assertEquals(1, events.disconnectCount.get())
        }

        fun assertPeerEventuallySeesEof() {
            peer.soTimeout = 3000
            // Some bytes may have been queued before shutdown (ping / TLS hello).
            val buffer = ByteArray(4096)
            while (input.read(buffer) != -1) { /* drain until the peer closes */ }
        }

        override fun close() {
            client.disconnect()
            peer.close()
            server.close()
        }
    }

    @Test(timeout = 10_000)
    fun defaultDeadlinesMatchTheProtocol() {
        val defaults = UbuDeskClient.Timeouts()
        assertEquals(120_000, defaults.pairingMs)
        assertEquals(6000, defaults.idleMs)
        assertEquals(2000L, defaults.pingIntervalMs)
    }

    @Test(timeout = 10_000)
    fun pinEntryDoesNotSendPingsOrUseStreamingIdleTimeout() {
        Connection().use { c ->
            c.beginPairing()
            // Late callbacks from a disposed decoder must not break pairing.
            c.client.send(Protocol.idr())
            c.client.send(Protocol.stats(30.0, 1.0, 0))
            c.peer.soTimeout = 800 // longer than idleMs; still within pairingMs
            try {
                c.input.readByte()
                throw AssertionError("client sent data while waiting for a PIN")
            } catch (_: SocketTimeoutException) {
                // Expected: no pre-auth heartbeat and no premature EOF.
            }
            assertTrue(c.events.disconnected.isEmpty())
            c.client.send(Protocol.authPin("123456"))
            assertEquals("auth", c.readControl().getString("t"))
            c.send("auth_ok")
            assertTrue(c.events.authenticated.await(1, TimeUnit.SECONDS))
            assertEquals("ping", c.readControl().getString("t"))
        }
    }

    @Test(timeout = 10_000)
    fun pairingWaitIsBounded() {
        Connection(UbuDeskClient.Timeouts(2000, 500, 200, 100)).use { c ->
            c.beginPairing()
            c.assertDisconnectedOnce()
            c.assertPeerEventuallySeesEof()
        }
    }

    @Test(timeout = 10_000)
    fun authenticatedHeartbeatsKeepPermissionDialogWaitAlive() {
        Connection().use { c ->
            c.authenticate()
            c.client.send(Protocol.start(320, 200, 30, 1000, "mirror", "touch"))
            var receivedStart = false
            // Wait longer than idleMs without sending started or video. The
            // server must still answer pings during a desktop permission dialog.
            repeat(8) {
                var msg = c.readControl()
                if (msg.getString("t") == "start") {
                    receivedStart = true
                    msg = c.readControl()
                }
                assertEquals("ping", msg.getString("t"))
                c.send("pong") { it.put("seq", msg.getInt("seq")) }
            }
            assertTrue(receivedStart)
            assertTrue(c.events.disconnected.isEmpty())
            c.send("started") {
                it.put("width", 320).put("height", 200).put("fps", 30).put("encoder", "test")
            }
            assertTrue(c.events.started.await(1, TimeUnit.SECONDS))
        }
    }

    @Test(timeout = 10_000)
    fun normalIdleTimeoutResumesAfterAuthentication() {
        Connection().use { c ->
            c.authenticate()
            // No pongs or video: pairing's longer timeout must no longer apply.
            c.assertDisconnectedOnce()
            c.assertPeerEventuallySeesEof()
        }
    }

    @Test(timeout = 10_000)
    fun failedAuthenticationClosesTheConnectionExactlyOnce() {
        Connection().use { c ->
            c.beginPairing()
            c.send("auth_fail") { it.put("reason", "bad_pin") }
            c.assertDisconnectedOnce()
            assertEquals("bad_pin", c.events.authFailure)
            c.assertPeerEventuallySeesEof()
        }
    }

    @Test(timeout = 10_000)
    fun readerEofClosesSocketAndWriter() {
        Connection().use { c ->
            c.beginPairing()
            c.peer.shutdownOutput() // half-close: client must close its half too
            c.assertDisconnectedOnce()
            c.assertPeerEventuallySeesEof()
        }
    }

    @Test(timeout = 10_000)
    fun cancelDuringTlsHandshakeClosesTheUnderlyingSocket() {
        Connection(tls = true).use { c ->
            // The peer accepts TCP but never completes the TLS handshake.
            c.client.disconnect("cancel pairing")
            c.assertDisconnectedOnce()
            c.assertPeerEventuallySeesEof()
        }
    }

    @Test(timeout = 10_000)
    fun simultaneousDisconnectsOnlyNotifyOnce() {
        Connection().use { c ->
            c.beginPairing()
            val threads = List(8) { Thread { c.client.disconnect() } }
            threads.forEach { it.start() }
            threads.forEach { it.join(1000) }
            assertFalse(threads.any { it.isAlive })
            c.assertDisconnectedOnce()
            c.assertPeerEventuallySeesEof()
        }
    }
}
