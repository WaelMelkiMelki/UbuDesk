package app.ubudesk.client.video

import android.media.MediaCodec
import android.media.MediaFormat
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.util.Log
import android.view.Surface
import app.ubudesk.client.net.Protocol
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.atomic.AtomicInteger

/**
 * Low-latency H.264 decoder rendering straight to a Surface.
 *
 * - Annex-B access units are fed as-is (SPS/PPS are in-band before every IDR).
 * - Output is released immediately (render=true), no timestamp scheduling.
 * - If more than [MAX_QUEUED] inputs are pending, non-key frames are dropped
 *   until the next keyframe and an IDR request callback fires.
 * - On codec errors the codec is recreated and an IDR is requested.
 */
class VideoDecoder(
    private val onNeedKeyframe: () -> Unit,
    private val onStats: (fps: Double, decodeMs: Double, dropped: Int) -> Unit,
    private val compatibilityMode: Boolean = false,
) {
    companion object {
        private const val TAG = "UbuDesk"
        private const val MIME = "video/avc"
        private const val MAX_QUEUED = 3
    }

    private var codec: MediaCodec? = null
    private var thread: HandlerThread? = null
    private var handler: Handler? = null
    private val pending = ConcurrentLinkedQueue<Protocol.Video>()
    private val pendingCount = AtomicInteger(0)
    @Volatile private var waitingForKeyframe = true
    @Volatile private var running = false
    private var surface: Surface? = null
    private var width = 0
    private var height = 0
    private var fps = 60

    // stats
    private var framesDecoded = 0
    private var framesDropped = 0
    private var decodeTimeAccumMs = 0.0
    private var lastStatsAt = 0L

    @Synchronized
    fun start(surface: Surface, width: Int, height: Int, fps: Int) {
        stop()
        this.surface = surface
        this.width = width
        this.height = height
        this.fps = fps
        running = true
        waitingForKeyframe = true
        thread = HandlerThread("ubudesk-decoder").also { it.start() }
        handler = Handler(thread!!.looper)
        handler!!.post { createCodec() }
    }

    private fun createCodec() {
        try {
            val format = MediaFormat.createVideoFormat(MIME, width, height)
            if (!compatibilityMode) {
                if (Build.VERSION.SDK_INT >= 30) {
                    try {
                        format.setInteger(MediaFormat.KEY_LOW_LATENCY, 1)
                    } catch (_: Exception) { /* not all codecs support it */ }
                }
                format.setInteger(MediaFormat.KEY_PRIORITY, 0) // realtime
                format.setFloat(MediaFormat.KEY_OPERATING_RATE, fps.toFloat())
            }
            val c = MediaCodec.createDecoderByType(MIME)
            c.configure(format, surface, null, 0)
            c.start()
            codec = c
            Log.i(TAG, "decoder started: ${c.name} ${width}x$height@$fps compat=$compatibilityMode")
            handler?.post(loop)
        } catch (e: Exception) {
            Log.e(TAG, "decoder creation failed", e)
            running = false
        }
    }

    /** Called from the network reader thread. */
    fun feed(frame: Protocol.Video) {
        if (!running) return
        if (waitingForKeyframe && !frame.isKeyframe) {
            framesDropped++
            return
        }
        if (pendingCount.get() >= MAX_QUEUED) {
            // Backlog: drop non-keys until the next keyframe, ask for an IDR.
            if (!frame.isKeyframe) {
                framesDropped++
                waitingForKeyframe = true
                onNeedKeyframe()
                return
            }
            // keyframe arrived: purge stale queue, keep the keyframe
            while (pending.poll() != null) pendingCount.decrementAndGet()
        }
        waitingForKeyframe = false
        pending.add(frame)
        pendingCount.incrementAndGet()
    }

    private val loop = object : Runnable {
        override fun run() {
            val c = codec ?: return
            if (!running) return
            try {
                // feed inputs
                var frame = pending.peek()
                while (frame != null) {
                    val inIndex = c.dequeueInputBuffer(0)
                    if (inIndex < 0) break
                    pending.poll()
                    pendingCount.decrementAndGet()
                    val buf = c.getInputBuffer(inIndex) ?: break
                    buf.clear()
                    buf.put(frame.data)
                    val flags = if (frame.hasConfig) MediaCodec.BUFFER_FLAG_KEY_FRAME else 0
                    val t0 = System.nanoTime()
                    c.queueInputBuffer(inIndex, 0, frame.data.size, frame.ptsUs, flags)
                    decodeTimeAccumMs += (System.nanoTime() - t0) / 1e6
                    frame = pending.peek()
                }
                // drain outputs, render ASAP
                val info = MediaCodec.BufferInfo()
                var outIndex = c.dequeueOutputBuffer(info, 0)
                while (outIndex >= 0) {
                    c.releaseOutputBuffer(outIndex, true)
                    framesDecoded++
                    outIndex = c.dequeueOutputBuffer(info, 0)
                }
                maybeReportStats()
            } catch (e: MediaCodec.CodecException) {
                Log.w(TAG, "codec exception (${e.diagnosticInfo}); recreating", e)
                recreate()
                return
            } catch (e: IllegalStateException) {
                Log.w(TAG, "codec illegal state; recreating", e)
                recreate()
                return
            }
            handler?.postDelayed(this, 2)
        }
    }

    private fun maybeReportStats() {
        val now = System.currentTimeMillis()
        if (lastStatsAt == 0L) lastStatsAt = now
        val elapsed = now - lastStatsAt
        if (elapsed >= 1000) {
            val fpsNow = framesDecoded * 1000.0 / elapsed
            val avgDecode = if (framesDecoded > 0) decodeTimeAccumMs / framesDecoded else 0.0
            onStats(fpsNow, avgDecode, framesDropped)
            framesDecoded = 0
            framesDropped = 0
            decodeTimeAccumMs = 0.0
            lastStatsAt = now
        }
    }

    private fun recreate() {
        if (!running) return
        runCatching { codec?.stop() }
        runCatching { codec?.release() }
        codec = null
        pending.clear()
        pendingCount.set(0)
        waitingForKeyframe = true
        onNeedKeyframe()
        handler?.postDelayed({ if (running) createCodec() }, 100)
    }

    @Synchronized
    fun stop() {
        running = false
        val h = handler
        val t = thread
        handler = null
        thread = null
        h?.removeCallbacksAndMessages(null)
        val c = codec
        codec = null
        if (h != null && t != null) {
            h.post {
                runCatching { c?.stop() }
                runCatching { c?.release() }
                t.quitSafely()
            }
        } else {
            runCatching { c?.stop() }
            runCatching { c?.release() }
            t?.quitSafely()
        }
        pending.clear()
        pendingCount.set(0)
    }
}
