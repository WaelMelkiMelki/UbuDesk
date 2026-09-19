package app.ubudesk.client.ui

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import app.ubudesk.client.MainViewModel
import app.ubudesk.client.UiState

@Composable
fun PairScreen(viewModel: MainViewModel, state: UiState.PairingPin) {
    var pin by remember { mutableStateOf("") }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(32.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Spacer(Modifier.height(32.dp))
        Text("Pair with ${state.server.name}", style = MaterialTheme.typography.headlineSmall)
        Spacer(Modifier.height(8.dp))
        Text(
            "Enter the 6-digit PIN shown in the `ubudesk serve` terminal on the PC.",
            style = MaterialTheme.typography.bodyMedium,
        )
        if (state.fingerprintShort != null) {
            Spacer(Modifier.height(8.dp))
            Text(
                "Security code: ${state.fingerprintShort} — it must match the code printed on the PC.",
                style = MaterialTheme.typography.bodySmall,
                fontFamily = FontFamily.Monospace,
                color = MaterialTheme.colorScheme.primary,
            )
        }
        Spacer(Modifier.height(24.dp))
        OutlinedTextField(
            value = pin,
            onValueChange = { pin = it.filter { c -> c.isDigit() }.take(6) },
            label = { Text("PIN") },
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.NumberPassword),
            singleLine = true,
        )
        Spacer(Modifier.height(16.dp))
        Row {
            TextButton(onClick = { viewModel.disconnect() }) { Text("Cancel") }
            Spacer(Modifier.width(16.dp))
            Button(enabled = pin.length == 6, onClick = { viewModel.submitPin(pin) }) {
                Text("Pair")
            }
        }
    }
}
