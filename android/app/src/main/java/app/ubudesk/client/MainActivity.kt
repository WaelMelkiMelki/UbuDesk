package app.ubudesk.client

import android.content.pm.ActivityInfo
import android.os.Bundle
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.viewModels
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import app.ubudesk.client.ui.ConnectScreen
import app.ubudesk.client.ui.PairScreen
import app.ubudesk.client.ui.SettingsScreen
import app.ubudesk.client.ui.StreamScreen
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue

class MainActivity : ComponentActivity() {

    private val viewModel: MainViewModel by viewModels()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        val (screenW, screenH, refresh) = if (android.os.Build.VERSION.SDK_INT >= 30) {
            val bounds = windowManager.currentWindowMetrics.bounds
            Triple(bounds.width(), bounds.height(), display?.refreshRate?.toInt() ?: 60)
        } else {
            @Suppress("DEPRECATION")
            val dm = android.util.DisplayMetrics().also {
                windowManager.defaultDisplay.getRealMetrics(it)
            }
            @Suppress("DEPRECATION")
            Triple(dm.widthPixels, dm.heightPixels, windowManager.defaultDisplay.refreshRate.toInt())
        }
        viewModel.setDeviceMetrics(
            maxOf(screenW, screenH),
            minOf(screenW, screenH),
            resources.displayMetrics.densityDpi,
            refresh,
        )

        setContent {
            MaterialTheme(colorScheme = darkColorScheme()) {
                val state by viewModel.state.collectAsState()
                val settings by viewModel.settings.collectAsState()
                var showSettings by remember { mutableStateOf(false) }

                // Landscape by default; portrait allowed when the user opts out.
                requestedOrientation = if (settings.lockLandscape) {
                    ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE
                } else {
                    ActivityInfo.SCREEN_ORIENTATION_FULL_SENSOR
                }

                when {
                    showSettings -> SettingsScreen(
                        viewModel = viewModel,
                        onBack = { showSettings = false },
                    )
                    state is UiState.Streaming -> {
                        Immersive(true)
                        StreamScreen(viewModel = viewModel, state = state as UiState.Streaming)
                    }
                    state is UiState.PairingPin -> {
                        Immersive(false)
                        PairScreen(viewModel = viewModel, state = state as UiState.PairingPin)
                    }
                    else -> {
                        Immersive(false)
                        ConnectScreen(
                            viewModel = viewModel,
                            state = state,
                            onOpenSettings = { showSettings = true },
                        )
                    }
                }
            }
        }
    }

    @androidx.compose.runtime.Composable
    private fun Immersive(enabled: Boolean) {
        androidx.compose.runtime.SideEffect {
            val controller = WindowCompat.getInsetsController(window, window.decorView)
            if (enabled) {
                controller.hide(WindowInsetsCompat.Type.systemBars())
                controller.systemBarsBehavior =
                    WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
            } else {
                controller.show(WindowInsetsCompat.Type.systemBars())
            }
        }
    }

    override fun onResume() {
        super.onResume()
        viewModel.startDiscovery()
    }

    override fun onPause() {
        super.onPause()
        viewModel.stopDiscovery()
    }
}
