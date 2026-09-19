package app.ubudesk.client.discovery

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.net.wifi.WifiManager
import android.util.Log

/**
 * NSD/mDNS discovery of `_ubudesk._tcp` servers on the LAN.
 * Holds a multicast lock while discovering (required on many devices).
 */
class ServerDiscovery(context: Context) {

    data class DiscoveredServer(
        val name: String,
        val host: String,
        val port: Int,
        val serverId: String?,
    )

    companion object {
        private const val TAG = "UbuDesk"
        private const val SERVICE_TYPE = "_ubudesk._tcp."
    }

    private val nsdManager = context.getSystemService(Context.NSD_SERVICE) as NsdManager
    private val wifiManager =
        context.applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
    private var multicastLock: WifiManager.MulticastLock? = null
    private var discoveryListener: NsdManager.DiscoveryListener? = null
    private var onFound: ((DiscoveredServer) -> Unit)? = null
    private var onLost: ((String) -> Unit)? = null

    fun start(onFound: (DiscoveredServer) -> Unit, onLost: (String) -> Unit) {
        stop()
        this.onFound = onFound
        this.onLost = onLost
        multicastLock = wifiManager.createMulticastLock("ubudesk-nsd").also {
            it.setReferenceCounted(false)
            it.acquire()
        }
        val listener = object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(serviceType: String) {
                Log.i(TAG, "NSD discovery started")
            }

            override fun onServiceFound(info: NsdServiceInfo) {
                resolve(info)
            }

            override fun onServiceLost(info: NsdServiceInfo) {
                onLost(info.serviceName)
            }

            override fun onDiscoveryStopped(serviceType: String) {}
            override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) {
                Log.w(TAG, "NSD start failed: $errorCode")
            }

            override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) {}
        }
        discoveryListener = listener
        try {
            nsdManager.discoverServices(SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, listener)
        } catch (e: Exception) {
            Log.w(TAG, "NSD discovery unavailable", e)
        }
    }

    @Suppress("DEPRECATION")
    private fun resolve(info: NsdServiceInfo) {
        try {
            nsdManager.resolveService(
                info,
                object : NsdManager.ResolveListener {
                    override fun onResolveFailed(serviceInfo: NsdServiceInfo, errorCode: Int) {
                        Log.w(TAG, "NSD resolve failed for ${serviceInfo.serviceName}: $errorCode")
                    }

                    override fun onServiceResolved(serviceInfo: NsdServiceInfo) {
                        val host = serviceInfo.host?.hostAddress ?: return
                        val serverId = serviceInfo.attributes?.get("id")?.let { String(it) }
                        onFound?.invoke(
                            DiscoveredServer(
                                name = serviceInfo.serviceName,
                                host = host,
                                port = serviceInfo.port,
                                serverId = serverId,
                            ),
                        )
                    }
                },
            )
        } catch (e: Exception) {
            Log.w(TAG, "NSD resolve threw", e)
        }
    }

    fun stop() {
        discoveryListener?.let {
            try {
                nsdManager.stopServiceDiscovery(it)
            } catch (_: Exception) {
            }
        }
        discoveryListener = null
        multicastLock?.let {
            if (it.isHeld) it.release()
        }
        multicastLock = null
    }
}
