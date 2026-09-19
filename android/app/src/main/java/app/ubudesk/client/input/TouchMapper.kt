package app.ubudesk.client.input

/**
 * Pure math for mapping view touch coordinates to normalized video
 * coordinates, accounting for letterboxing. Unit-tested on the JVM.
 */
object TouchMapper {

    /** The rectangle (in view pixels) that the video occupies inside the view. */
    data class VideoRect(val left: Float, val top: Float, val width: Float, val height: Float)

    /**
     * Computes the video rectangle for aspect-fit rendering of a
     * [videoW]x[videoH] stream inside a [viewW]x[viewH] view.
     */
    fun videoRect(viewW: Float, viewH: Float, videoW: Float, videoH: Float): VideoRect {
        if (viewW <= 0 || viewH <= 0 || videoW <= 0 || videoH <= 0) {
            return VideoRect(0f, 0f, viewW.coerceAtLeast(0f), viewH.coerceAtLeast(0f))
        }
        val viewAspect = viewW / viewH
        val videoAspect = videoW / videoH
        return if (videoAspect > viewAspect) {
            // video is wider: bars top+bottom
            val h = viewW / videoAspect
            VideoRect(0f, (viewH - h) / 2f, viewW, h)
        } else {
            // video is taller: bars left+right
            val w = viewH * videoAspect
            VideoRect((viewW - w) / 2f, 0f, w, viewH)
        }
    }

    /**
     * Maps a view-space point to normalized 0..1 video coordinates.
     * Returns null when the point is outside the video rectangle.
     */
    fun normalize(rect: VideoRect, x: Float, y: Float): Pair<Double, Double>? {
        if (rect.width <= 0 || rect.height <= 0) return null
        val nx = (x - rect.left) / rect.width
        val ny = (y - rect.top) / rect.height
        if (nx < 0f || nx > 1f || ny < 0f || ny > 1f) return null
        return nx.toDouble() to ny.toDouble()
    }

    /** Clamped variant used for MOVE events of an already-tracked pointer. */
    fun normalizeClamped(rect: VideoRect, x: Float, y: Float): Pair<Double, Double> {
        val nx = ((x - rect.left) / rect.width).coerceIn(0f, 1f)
        val ny = ((y - rect.top) / rect.height).coerceIn(0f, 1f)
        return nx.toDouble() to ny.toDouble()
    }
}
