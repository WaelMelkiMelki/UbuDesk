package app.ubudesk.client.input

import android.view.MotionEvent
import app.ubudesk.client.net.Protocol
import org.json.JSONObject
import kotlin.math.abs

/**
 * Converts MotionEvents into protocol messages.
 *
 * Touch mode: every pointer becomes a `touch` stream (id = pointerId).
 * Mouse mode: one finger = absolute left-button mouse; two-finger tap =
 * right click; two-finger vertical/horizontal drag = scroll.
 *
 * MOVE messages are rate-limited to ~120/s per pointer.
 */
class TouchForwarder(
    private val send: (JSONObject) -> Unit,
    var touchMode: Boolean = true,
) {
    var rect = TouchMapper.VideoRect(0f, 0f, 0f, 0f)

    private val lastMoveAt = HashMap<Int, Long>()
    private val tracked = HashSet<Int>()
    private val moveIntervalMs = 8L // ~120 Hz

    // mouse mode state
    private var mouseDown = false
    private var twoFingerActive = false
    private var twoFingerMoved = false
    private var twoFingerStartY = 0f
    private var twoFingerStartX = 0f
    private var scrollAccumY = 0f
    private var scrollAccumX = 0f
    private val scrollStepPx = 64f

    fun onTouch(event: MotionEvent): Boolean {
        return if (touchMode) onTouchMultitouch(event) else onTouchMouse(event)
    }

    // ---------------------------------------------------------------- touch

    private fun onTouchMultitouch(event: MotionEvent): Boolean {
        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN, MotionEvent.ACTION_POINTER_DOWN -> {
                val idx = event.actionIndex
                val id = event.getPointerId(idx)
                if (id >= 10) return true
                val n = TouchMapper.normalize(rect, event.getX(idx), event.getY(idx)) ?: return true
                tracked.add(id)
                send(Protocol.touch("down", id, n.first, n.second))
            }
            MotionEvent.ACTION_MOVE -> {
                val now = System.currentTimeMillis()
                for (idx in 0 until event.pointerCount) {
                    val id = event.getPointerId(idx)
                    if (id !in tracked) continue
                    if (now - (lastMoveAt[id] ?: 0) < moveIntervalMs) continue
                    lastMoveAt[id] = now
                    val n = TouchMapper.normalizeClamped(rect, event.getX(idx), event.getY(idx))
                    send(Protocol.touch("move", id, n.first, n.second))
                }
            }
            MotionEvent.ACTION_UP, MotionEvent.ACTION_POINTER_UP -> {
                val idx = event.actionIndex
                val id = event.getPointerId(idx)
                if (id in tracked) {
                    tracked.remove(id)
                    val n = TouchMapper.normalizeClamped(rect, event.getX(idx), event.getY(idx))
                    send(Protocol.touch("up", id, n.first, n.second))
                }
            }
            MotionEvent.ACTION_CANCEL -> {
                for (id in tracked) send(Protocol.touch("cancel", id, 0.0, 0.0))
                tracked.clear()
            }
        }
        return true
    }

    // ---------------------------------------------------------------- mouse

    private fun onTouchMouse(event: MotionEvent): Boolean {
        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                val n = TouchMapper.normalize(rect, event.x, event.y) ?: return true
                mouseDown = true
                twoFingerActive = false
                send(Protocol.mouse("down", "left", n.first, n.second))
            }
            MotionEvent.ACTION_POINTER_DOWN -> {
                if (event.pointerCount == 2) {
                    // second finger: cancel the left press, start scroll/right-click gesture
                    if (mouseDown) {
                        val n = TouchMapper.normalizeClamped(rect, event.getX(0), event.getY(0))
                        send(Protocol.mouse("up", "left", n.first, n.second))
                        mouseDown = false
                    }
                    twoFingerActive = true
                    twoFingerMoved = false
                    twoFingerStartX = (event.getX(0) + event.getX(1)) / 2f
                    twoFingerStartY = (event.getY(0) + event.getY(1)) / 2f
                    scrollAccumX = 0f
                    scrollAccumY = 0f
                }
            }
            MotionEvent.ACTION_MOVE -> {
                if (twoFingerActive && event.pointerCount >= 2) {
                    val cx = (event.getX(0) + event.getX(1)) / 2f
                    val cy = (event.getY(0) + event.getY(1)) / 2f
                    scrollAccumY += cy - twoFingerStartY
                    scrollAccumX += cx - twoFingerStartX
                    twoFingerStartY = cy
                    twoFingerStartX = cx
                    var stepsY = 0
                    var stepsX = 0
                    while (scrollAccumY <= -scrollStepPx) { stepsY++; scrollAccumY += scrollStepPx }
                    while (scrollAccumY >= scrollStepPx) { stepsY--; scrollAccumY -= scrollStepPx }
                    while (scrollAccumX <= -scrollStepPx) { stepsX--; scrollAccumX += scrollStepPx }
                    while (scrollAccumX >= scrollStepPx) { stepsX++; scrollAccumX -= scrollStepPx }
                    if (stepsY != 0 || stepsX != 0) {
                        twoFingerMoved = true
                        // content follows fingers: fingers up => scroll down (+dy)
                        send(Protocol.scroll(stepsX.toDouble(), stepsY.toDouble()))
                    }
                    if (abs(cx - twoFingerStartX) > 24 || abs(cy - twoFingerStartY) > 24) {
                        twoFingerMoved = true
                    }
                } else if (mouseDown) {
                    val now = System.currentTimeMillis()
                    if (now - (lastMoveAt[0] ?: 0) >= moveIntervalMs) {
                        lastMoveAt[0] = now
                        val n = TouchMapper.normalizeClamped(rect, event.x, event.y)
                        send(Protocol.mouse("move", null, n.first, n.second))
                    }
                }
            }
            MotionEvent.ACTION_POINTER_UP -> {
                if (twoFingerActive && event.pointerCount == 2) {
                    if (!twoFingerMoved) {
                        // two-finger tap = right click at the midpoint
                        val cx = (event.getX(0) + event.getX(1)) / 2f
                        val cy = (event.getY(0) + event.getY(1)) / 2f
                        val n = TouchMapper.normalizeClamped(rect, cx, cy)
                        send(Protocol.mouse("down", "right", n.first, n.second))
                        send(Protocol.mouse("up", "right", n.first, n.second))
                    }
                    twoFingerActive = false
                }
            }
            MotionEvent.ACTION_UP -> {
                if (mouseDown) {
                    val n = TouchMapper.normalizeClamped(rect, event.x, event.y)
                    send(Protocol.mouse("up", "left", n.first, n.second))
                    mouseDown = false
                }
                twoFingerActive = false
            }
            MotionEvent.ACTION_CANCEL -> {
                if (mouseDown) {
                    send(Protocol.mouse("up", "left", 0.5, 0.5))
                    mouseDown = false
                }
                twoFingerActive = false
            }
        }
        return true
    }
}
