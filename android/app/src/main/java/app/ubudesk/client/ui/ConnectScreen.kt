package app.ubudesk.client.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import app.ubudesk.client.MainViewModel
import app.ubudesk.client.UiState
import app.ubudesk.client.data.KnownServer

@Composable
fun ConnectScreen(viewModel: MainViewModel, state: UiState, onOpenSettings: () -> Unit) {
    val discovered by viewModel.discovered.collectAsState()
    val known by viewModel.knownServers.collectAsState()
    var manualHost by remember { mutableStateOf("") }
    var manualPort by remember { mutableStateOf("7777") }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(24.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("UbuDesk", style = MaterialTheme.typography.headlineMedium)
            Spacer(Modifier.width(16.dp))
            Text(
                "Android display for your Ubuntu PC",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Spacer(Modifier.weight(1f))
            TextButton(onClick = onOpenSettings) { Text("Settings") }
        }

        if (state is UiState.Failed) {
            Spacer(Modifier.height(8.dp))
            Card {
                Text(
                    state.message,
                    modifier = Modifier.padding(12.dp),
                    color = MaterialTheme.colorScheme.error,
                )
            }
        }
        if (state is UiState.Connecting) {
            Spacer(Modifier.height(8.dp))
            Row(verticalAlignment = Alignment.CenterVertically) {
                CircularProgressIndicator(Modifier.width(20.dp).height(20.dp))
                Spacer(Modifier.width(12.dp))
                Text(state.status)
                Spacer(Modifier.width(12.dp))
                TextButton(onClick = { viewModel.disconnect() }) { Text("Cancel") }
            }
        }

        Spacer(Modifier.height(16.dp))

        LazyColumn(
            modifier = Modifier.weight(1f),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            if (known.isNotEmpty()) {
                item { Text("Paired servers", style = MaterialTheme.typography.titleMedium) }
                items(known, key = { "k" + it.key }) { server ->
                    ServerRow(
                        title = server.name,
                        subtitle = "${server.host}:${server.port}" +
                            if (server.token != null) "  •  paired" else "",
                        onConnect = { viewModel.connectTo(server) },
                        onForget = { viewModel.forgetServer(server.key) },
                    )
                }
            }
            item { Text("Discovered on this network", style = MaterialTheme.typography.titleMedium) }
            if (discovered.isEmpty()) {
                item {
                    Text(
                        "Searching for _ubudesk._tcp servers… Start `ubudesk serve` on your PC.",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
            items(discovered, key = { "d" + it.name }) { server ->
                val existing = known.find { k -> k.host == server.host && k.port == server.port }
                ServerRow(
                    title = server.name,
                    subtitle = "${server.host}:${server.port}",
                    onConnect = {
                        viewModel.connectTo(
                            existing ?: KnownServer(server.host, server.port, server.name, null, null),
                        )
                    },
                    onForget = null,
                )
            }
        }

        Spacer(Modifier.height(8.dp))
        Text("Add manually", style = MaterialTheme.typography.titleMedium)
        Spacer(Modifier.height(8.dp))
        Row(verticalAlignment = Alignment.CenterVertically) {
            OutlinedTextField(
                value = manualHost,
                onValueChange = { manualHost = it },
                label = { Text("Host or IP") },
                modifier = Modifier.weight(1f),
                singleLine = true,
            )
            Spacer(Modifier.width(8.dp))
            OutlinedTextField(
                value = manualPort,
                onValueChange = { manualPort = it.filter { ch -> ch.isDigit() }.take(5) },
                label = { Text("Port") },
                modifier = Modifier.width(110.dp),
                singleLine = true,
            )
            Spacer(Modifier.width(8.dp))
            Button(
                enabled = manualHost.isNotBlank(),
                onClick = {
                    val port = manualPort.toIntOrNull() ?: 7777
                    viewModel.connectTo(KnownServer(manualHost.trim(), port, manualHost.trim(), null, null))
                },
            ) { Text("Connect") }
            Spacer(Modifier.width(8.dp))
            OutlinedButton(onClick = {
                viewModel.connectTo(KnownServer("127.0.0.1", 7777, "USB (adb reverse)", null, null))
            }) { Text("USB") }
        }
        Text(
            "USB mode: enable USB debugging, connect the cable, run `ubudesk usb` on the PC, then tap USB.",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}

@Composable
private fun ServerRow(
    title: String,
    subtitle: String,
    onConnect: () -> Unit,
    onForget: (() -> Unit)?,
) {
    Card(modifier = Modifier.fillMaxWidth()) {
        Row(
            modifier = Modifier.padding(horizontal = 16.dp, vertical = 10.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column(Modifier.weight(1f)) {
                Text(title, style = MaterialTheme.typography.titleSmall)
                Text(
                    subtitle,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            if (onForget != null) {
                TextButton(onClick = onForget) { Text("Forget") }
                Spacer(Modifier.width(4.dp))
            }
            Button(onClick = onConnect) { Text("Connect") }
        }
    }
}
