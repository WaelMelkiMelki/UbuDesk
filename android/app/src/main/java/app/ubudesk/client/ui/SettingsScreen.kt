package app.ubudesk.client.ui

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Slider
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import app.ubudesk.client.MainViewModel

@OptIn(ExperimentalLayoutApi::class)
@Composable
fun SettingsScreen(viewModel: MainViewModel, onBack: () -> Unit) {
    val settings by viewModel.settings.collectAsState()

    Column(
        Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(24.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            TextButton(onClick = onBack) { Text("← Back") }
            Spacer(Modifier.width(8.dp))
            Text("Settings", style = MaterialTheme.typography.headlineSmall)
        }
        Spacer(Modifier.height(16.dp))

        Text("Mode", style = MaterialTheme.typography.titleMedium)
        Row {
            for (mode in listOf("extend", "mirror")) {
                FilterChip(
                    selected = settings.mode == mode,
                    onClick = { viewModel.saveSettings(settings.copy(mode = mode)) },
                    label = { Text(mode) },
                    modifier = Modifier.padding(end = 8.dp),
                )
            }
        }
        Text(
            "extend = new virtual monitor; mirror = share an existing one",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.height(16.dp))

        Text("Resolution", style = MaterialTheme.typography.titleMedium)
        FlowRow {
            for (preset in listOf("auto", "native", "1920x1200", "1600x1000", "1280x800")) {
                FilterChip(
                    selected = settings.resolutionPreset == preset,
                    onClick = { viewModel.saveSettings(settings.copy(resolutionPreset = preset)) },
                    label = { Text(preset) },
                    modifier = Modifier.padding(end = 8.dp),
                )
            }
        }
        Text(
            "auto = 1920-wide on tablets, 1280-wide on phones (matched to this screen's aspect)",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.height(16.dp))

        Text("Frame rate", style = MaterialTheme.typography.titleMedium)
        Row {
            for (fps in listOf(30, 60)) {
                FilterChip(
                    selected = settings.fps == fps,
                    onClick = { viewModel.saveSettings(settings.copy(fps = fps)) },
                    label = { Text("$fps fps") },
                    modifier = Modifier.padding(end = 8.dp),
                )
            }
        }
        Spacer(Modifier.height(16.dp))

        Text(
            "Bitrate: ${settings.bitrateKbps / 1000} Mbps",
            style = MaterialTheme.typography.titleMedium,
        )
        Slider(
            value = settings.bitrateKbps.toFloat(),
            onValueChange = { viewModel.saveSettings(settings.copy(bitrateKbps = (it / 500).toInt() * 500)) },
            valueRange = 5000f..40000f,
        )
        Spacer(Modifier.height(16.dp))

        SettingSwitch(
            title = "Touch mode",
            subtitle = "On: fingers act as touchscreen. Off: single-finger mouse with two-finger scroll/right-click.",
            checked = settings.touchMode,
            onChange = { viewModel.saveSettings(settings.copy(touchMode = it)) },
        )
        SettingSwitch(
            title = "Stats overlay",
            subtitle = "Show fps / decode time / drops during streaming.",
            checked = settings.showStats,
            onChange = { viewModel.saveSettings(settings.copy(showStats = it)) },
        )
        SettingSwitch(
            title = "Compatibility decode",
            subtitle = "Disable low-latency decoder hints. Try this if the video never appears.",
            checked = settings.compatibilityDecode,
            onChange = { viewModel.saveSettings(settings.copy(compatibilityDecode = it)) },
        )
        SettingSwitch(
            title = "Landscape only",
            subtitle = "Keep the app in landscape (recommended). Off allows portrait too.",
            checked = settings.lockLandscape,
            onChange = { viewModel.saveSettings(settings.copy(lockLandscape = it)) },
        )
    }
}

@Composable
private fun SettingSwitch(
    title: String,
    subtitle: String,
    checked: Boolean,
    onChange: (Boolean) -> Unit,
) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = Modifier.padding(vertical = 6.dp),
    ) {
        Column(Modifier.weight(1f)) {
            Text(title, style = MaterialTheme.typography.titleMedium)
            Text(
                subtitle,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        Switch(checked = checked, onCheckedChange = onChange)
    }
}
