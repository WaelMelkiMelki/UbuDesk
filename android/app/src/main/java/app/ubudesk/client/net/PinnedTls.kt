package app.ubudesk.client.net

import java.security.MessageDigest
import java.security.cert.CertificateException
import java.security.cert.X509Certificate
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLSocketFactory
import javax.net.ssl.X509TrustManager

/**
 * TLS with trust-on-first-use certificate pinning.
 *
 * - During pairing (no stored fingerprint yet) any certificate is accepted;
 *   the caller MUST read [capturedFingerprint] after the handshake and store
 *   it together with the auth token, and should display the short code so the
 *   user can compare it with the server console.
 * - After pairing, only the exact pinned SHA-256 leaf fingerprint is accepted.
 */
class PinnedTrustManager(private val pinnedFingerprintHex: String?) : X509TrustManager {

    @Volatile
    var capturedFingerprint: String? = null
        private set

    override fun checkClientTrusted(chain: Array<X509Certificate>, authType: String) {
        throw CertificateException("client certificates not supported")
    }

    override fun checkServerTrusted(chain: Array<X509Certificate>, authType: String) {
        if (chain.isEmpty()) throw CertificateException("empty certificate chain")
        val actual = fingerprintOf(chain[0])
        capturedFingerprint = actual
        val pinned = pinnedFingerprintHex
        if (pinned != null && !constantTimeEquals(actual, pinned.lowercase())) {
            throw CertificateException(
                "server certificate fingerprint mismatch: expected ${shortCode(pinned)}, " +
                    "got ${shortCode(actual)} - possible MITM or reinstalled server",
            )
        }
    }

    override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()

    companion object {
        fun fingerprintOf(cert: X509Certificate): String =
            MessageDigest.getInstance("SHA-256").digest(cert.encoded)
                .joinToString("") { "%02x".format(it) }

        fun shortCode(fingerprintHex: String): String =
            fingerprintHex.take(8).uppercase()

        fun constantTimeEquals(a: String, b: String): Boolean {
            if (a.length != b.length) return false
            var result = 0
            for (i in a.indices) result = result or (a[i].code xor b[i].code)
            return result == 0
        }

        fun socketFactory(trustManager: PinnedTrustManager): SSLSocketFactory {
            val ctx = SSLContext.getInstance("TLSv1.2")
            ctx.init(null, arrayOf(trustManager), java.security.SecureRandom())
            return ctx.socketFactory
        }
    }
}
