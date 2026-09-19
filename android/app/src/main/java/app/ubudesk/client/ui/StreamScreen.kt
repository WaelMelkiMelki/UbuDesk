package app.ubudesk.client.ui

import android.view.SurfaceHolder
import android.view.SurfaceView
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import app.ubudesk.client.MainViewModel
import app.ubudesk.client.UiState
import app.ubudesk.client.input.TouchForwarder
import app.ubudesk.client.input.TouchMapper
import app.ubudesk.client.video.VideoDecoder

@Composable
fun StreamScreen(viewModel: MainViewModel, state: UiState.Streaming) {
    var overlayVisible by remember { mutableStateOf(true) }
    val settings by viewModel.settings.collectAsState()
    val stats by viewModel.stats.collectAsState()

    val decoder = remember(state) {
        VideoDecoder(
            onNeedKeyframe = { viewModel.requestIdr() },
            onStats = { fps, ms, dropped -> viewModel.sendStats(fps, ms, dropped) },
            compatibilityMode = settings.compatibilityDecode,
        )
    }
    val forwarder = remember(state) {
        TouchForwarder(
            send = { msg -> viewModel.client?.send(msg) },
            touchMode = settings.touchMode,
        )
    }

    DisposableEffect(state) {
        viewModel.onVideoFrame = { frame -> decoder.feed(frame) }
        onDispose {
            viewModel.onVideoFrame = null
            decoder.stop()
        }
    }

    Box(Modifier.fillMaxSize().background(Color.Black)) {
        AndroidView(
            modifier = Modifier.fillMaxSize(),
            factory = { context ->
                SurfaceView(context).apply {
                    holder.addCallback(object : SurfaceHolder.Callback {
                        override fun surfaceCreated(holder: SurfaceHolder) {
                            decoder.start(holder.surface, state.width, state.height, state.fps)
                            viewModel.requestIdr()
                        }

                        override fun surfaceChanged(
                            holder: SurfaceHolder,
                            format: Int,
                            width: Int,
                            height: Int,
                        ) {
                            forwarder.rect = TouchMapper.videoRect(
                                width.toFloat(), height.toFloat(),
                                state.width.toFloat(), state.height.toFloat(),
                            )
                        }

                        override fun surfaceDestroyed(holder: SurfaceHolder) {
                            decoder.stop()
                        }
                    })
                    setOnTouchListener { v, event ->
                        overlayVisible = false
                        val handled = forwarder.onTouch(event)
                        if (event.actionMasked == android.view.MotionEvent.ACTION_UP) {
                            v.performClick()
                        }
                        handled
                    }
                }
            },
        )

        // small overlay toggle zone: tap top-left corner to reveal controls
        Box(
            Modifier
                .align(Alignment.TopStart)
                .width(48.dp),
        ) {
            TextButton(onClick = { overlayVisible = !overlayVisible }) { Text("☰") }
        }

        if (overlayVisible) {
            Surface(
                modifier = Modifier
                    .align(Alignment.TopCenter)
                    .padding(top = 8.dp),
                color = Color(0xCC101010),
                shape = MaterialTheme.shapes.medium,
            ) {
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    modifier = Modifier.padding(horizontal = 12.dp, vertical = 2.dp),
                ) {
                    Text(
                        "${state.server.name} • ${state.width}x${state.height}@${state.fps} • ${state.encoder} • ${settings.mode}",
                        style = MaterialTheme.typography.bodySmall,
                        color = Color.White,
                    )
                    Spacer(Modifier.width(12.dp))
                    TextButton(onClick = {
                        forwarder.touchMode = !forwarder.touchMode
                    }) { Text(if (forwarder.touchMode) "Touch" else "Mouse") }
                    TextButton(onClick = { viewModel.requestIdr() }) { Text("Refresh") }
                    TextButton(onClick = { viewModel.disconnect() }) {
                        Text("Disconnect", color = MaterialTheme.colorScheme.error)
                    }
                }
            }
        }

        if (settings.showStats && stats.isNotEmpty()) {
            Column(
                Modifier
                    .align(Alignment.BottomStart)
                    .padding(8.dp)
                    .background(Color(0x99000000)),
            ) {
                Text(
                    stats,
                    color = Color.Green,
                    fontFamily = FontFamily.Monospace,
                    style = MaterialTheme.typography.bodySmall,
                    modifier = Modifier.padding(4.dp),
                )
            }
        }
    }
}
