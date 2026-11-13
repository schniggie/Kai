package com.inspiredandroid.kai.ui.settings

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.selection.selectable
import androidx.compose.foundation.selection.selectableGroup
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment.Companion.CenterVertically
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.unit.dp
import com.inspiredandroid.kai.formatFileSize
import com.inspiredandroid.kai.linux.LinuxDistro
import com.inspiredandroid.kai.ui.KaiOutlinedTextField
import com.inspiredandroid.kai.ui.handCursor
import com.inspiredandroid.kai.ui.sandbox.SandboxProgressRow
import com.inspiredandroid.kai.ui.sandbox.sandboxStatusText
import kai.composeapp.generated.resources.Res
import kai.composeapp.generated.resources.settings_sandbox_cancel
import kai.composeapp.generated.resources.settings_sandbox_description
import kai.composeapp.generated.resources.settings_sandbox_disk_usage
import kai.composeapp.generated.resources.settings_sandbox_distro_alpine
import kai.composeapp.generated.resources.settings_sandbox_distro_alpine_detail
import kai.composeapp.generated.resources.settings_sandbox_distro_alpine_installed
import kai.composeapp.generated.resources.settings_sandbox_distro_debian
import kai.composeapp.generated.resources.settings_sandbox_distro_debian_detail
import kai.composeapp.generated.resources.settings_sandbox_distro_debian_installed
import kai.composeapp.generated.resources.settings_sandbox_distro_title
import kai.composeapp.generated.resources.settings_sandbox_install
import kai.composeapp.generated.resources.settings_sandbox_install_packages
import kai.composeapp.generated.resources.settings_sandbox_migrate_action
import kai.composeapp.generated.resources.settings_sandbox_migrate_detail
import kai.composeapp.generated.resources.settings_sandbox_migrate_title
import kai.composeapp.generated.resources.settings_sandbox_remote_connect
import kai.composeapp.generated.resources.settings_sandbox_remote_description
import kai.composeapp.generated.resources.settings_sandbox_remote_device_id
import kai.composeapp.generated.resources.settings_sandbox_remote_disconnect
import kai.composeapp.generated.resources.settings_sandbox_remote_title
import kai.composeapp.generated.resources.settings_sandbox_remote_token
import kai.composeapp.generated.resources.settings_sandbox_remote_url
import kai.composeapp.generated.resources.settings_sandbox_uninstall
import kai.composeapp.generated.resources.settings_sandbox_uninstall_confirm
import kai.composeapp.generated.resources.settings_sandbox_uninstall_confirm_shared
import org.jetbrains.compose.resources.stringResource

@Composable
internal fun SandboxSettingsCard(
    sandboxState: SandboxUiState,
    onToggleSandbox: (Boolean) -> Unit,
    onSelectDistro: (LinuxDistro) -> Unit,
    onSetupSandbox: () -> Unit,
    onCancelSandbox: () -> Unit,
    onResetSandbox: () -> Unit,
    onInstallPackages: () -> Unit,
    onMigrateHome: () -> Unit,
    onRemoteUrlChange: (String) -> Unit = {},
    onRemoteTokenChange: (String) -> Unit = {},
    onRemoteDeviceIdChange: (String) -> Unit = {},
) {
    var showResetDialog by remember { mutableStateOf(false) }
    val statusText = sandboxStatusText(sandboxState.sandboxStatusLabel)
    SettingsCard {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = CenterVertically,
        ) {
            Column(modifier = Modifier.weight(1f)) {
                Text(
                    text = if (sandboxState.isRemote) {
                        stringResource(Res.string.settings_sandbox_remote_title)
                    } else {
                        sandboxState.distro.displayName
                    },
                    style = MaterialTheme.typography.titleMedium,
                    color = MaterialTheme.colorScheme.onBackground,
                )
                if (sandboxState.sandboxReady) {
                    if (sandboxState.sandboxDiskUsageMB > 0) {
                        Text(
                            text = stringResource(Res.string.settings_sandbox_disk_usage, sandboxState.sandboxDiskUsageMB),
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                } else {
                    Spacer(Modifier.height(4.dp))
                    Text(
                        text = stringResource(
                            if (sandboxState.isRemote) {
                                Res.string.settings_sandbox_remote_description
                            } else {
                                Res.string.settings_sandbox_description
                            },
                        ),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
            if (sandboxState.sandboxReady) {
                Switch(
                    checked = sandboxState.isSandboxEnabled,
                    onCheckedChange = onToggleSandbox,
                )
            }
        }

        if (sandboxState.isRemote) {
            Spacer(Modifier.height(12.dp))
            KaiOutlinedTextField(
                value = sandboxState.remoteUrl,
                onValueChange = onRemoteUrlChange,
                label = { Text(stringResource(Res.string.settings_sandbox_remote_url)) },
                modifier = Modifier.fillMaxWidth(),
                singleLine = true,
                enabled = !sandboxState.isWorking,
            )
            Spacer(Modifier.height(8.dp))
            KaiOutlinedTextField(
                value = sandboxState.remoteToken,
                onValueChange = onRemoteTokenChange,
                label = { Text(stringResource(Res.string.settings_sandbox_remote_token)) },
                modifier = Modifier.fillMaxWidth(),
                singleLine = true,
                enabled = !sandboxState.isWorking,
            )
            Spacer(Modifier.height(8.dp))
            KaiOutlinedTextField(
                value = sandboxState.remoteDeviceId,
                onValueChange = onRemoteDeviceIdChange,
                label = { Text(stringResource(Res.string.settings_sandbox_remote_device_id)) },
                modifier = Modifier.fillMaxWidth(),
                singleLine = true,
                enabled = !sandboxState.isWorking,
            )
        } else if (!sandboxState.isWorking) {
            // Local Android: each distribution keeps its own install.
            DistroPicker(
                selected = sandboxState.distro,
                installed = sandboxState.installedDistros,
                onSelect = onSelectDistro,
            )
        }

        if (sandboxState.sandboxProgress != null) {
            SandboxProgressRow(sandboxState.sandboxProgress, statusText, onCancelSandbox)
        } else if (sandboxState.isWorking) {
            SandboxProgressRow(null, statusText, onCancelSandbox)
        } else if (sandboxState.hasError) {
            Spacer(Modifier.height(8.dp))
            Text(
                text = statusText,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.error,
            )
        }

        // Offered whenever the distribution being left still holds files this one
        // does not, so switching never has to mean abandoning them. It stops
        // being shown once the copy is done, because then there is nothing left.
        val migration = sandboxState.migration
        if (migration != null && sandboxState.sandboxReady && !sandboxState.isWorking) {
            Spacer(Modifier.height(12.dp))
            Text(
                text = stringResource(
                    Res.string.settings_sandbox_migrate_title,
                    migration.from.displayName,
                ),
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onBackground,
            )
            Text(
                text = stringResource(
                    Res.string.settings_sandbox_migrate_detail,
                    migration.fileCount,
                    formatFileSize(migration.bytes),
                ),
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Spacer(Modifier.height(8.dp))
            // Its own button rather than a third one in the action row below:
            // that row is already two wide on a phone, and this belongs to the
            // sentence above it.
            OutlinedButton(onClick = onMigrateHome, modifier = Modifier.handCursor()) {
                Text(stringResource(Res.string.settings_sandbox_migrate_action))
            }
        }

        if (!sandboxState.isWorking) {
            Spacer(Modifier.height(8.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                if (!sandboxState.sandboxReady) {
                    Button(onClick = onSetupSandbox, modifier = Modifier.handCursor()) {
                        Text(
                            stringResource(
                                if (sandboxState.isRemote) {
                                    Res.string.settings_sandbox_remote_connect
                                } else {
                                    Res.string.settings_sandbox_install
                                },
                            ),
                        )
                    }
                } else {
                    if (!sandboxState.sandboxPackagesInstalled) {
                        OutlinedButton(onClick = onInstallPackages, modifier = Modifier.handCursor()) {
                            Text(stringResource(Res.string.settings_sandbox_install_packages))
                        }
                    }
                    OutlinedButton(onClick = { showResetDialog = true }, modifier = Modifier.handCursor()) {
                        Text(
                            stringResource(
                                if (sandboxState.isRemote) {
                                    Res.string.settings_sandbox_remote_disconnect
                                } else {
                                    Res.string.settings_sandbox_uninstall
                                },
                            ),
                        )
                    }
                }
            }
        }
    }

    if (showResetDialog) {
        AlertDialog(
            onDismissRequest = { showResetDialog = false },
            title = {
                Text(
                    stringResource(
                        if (sandboxState.isRemote) {
                            Res.string.settings_sandbox_remote_disconnect
                        } else {
                            Res.string.settings_sandbox_uninstall
                        },
                    ),
                )
            },
            text = {
                val confirm = when {
                    sandboxState.isRemote -> Res.string.settings_sandbox_uninstall_confirm
                    sandboxState.distro == LinuxDistro.DEBIAN -> Res.string.settings_sandbox_uninstall_confirm_shared
                    else -> Res.string.settings_sandbox_uninstall_confirm
                }
                Text(stringResource(confirm))
            },
            confirmButton = {
                TextButton(
                    onClick = {
                        showResetDialog = false
                        onResetSandbox()
                    },
                    modifier = Modifier.handCursor(),
                ) {
                    Text(
                        stringResource(
                            if (sandboxState.isRemote) {
                                Res.string.settings_sandbox_remote_disconnect
                            } else {
                                Res.string.settings_sandbox_uninstall
                            },
                        ),
                    )
                }
            },
            dismissButton = {
                TextButton(
                    onClick = { showResetDialog = false },
                    modifier = Modifier.handCursor(),
                ) {
                    Text(stringResource(Res.string.settings_sandbox_cancel))
                }
            },
        )
    }
}

@Composable
private fun DistroPicker(
    selected: LinuxDistro,
    installed: Set<LinuxDistro>,
    onSelect: (LinuxDistro) -> Unit,
) {
    Spacer(Modifier.height(12.dp))
    Text(
        text = stringResource(Res.string.settings_sandbox_distro_title),
        style = MaterialTheme.typography.labelLarge,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
    )
    Column(Modifier.selectableGroup()) {
        DistroOption(
            distro = LinuxDistro.DEBIAN,
            title = stringResource(Res.string.settings_sandbox_distro_debian),
            // An install already on disk is a switch, not a download — the size
            // estimate would be the wrong thing to lead with.
            detail = if (LinuxDistro.DEBIAN in installed) {
                stringResource(Res.string.settings_sandbox_distro_debian_installed)
            } else {
                stringResource(Res.string.settings_sandbox_distro_debian_detail)
            },
            selected = selected == LinuxDistro.DEBIAN,
            onSelect = onSelect,
        )
        DistroOption(
            distro = LinuxDistro.ALPINE,
            title = stringResource(Res.string.settings_sandbox_distro_alpine),
            detail = if (LinuxDistro.ALPINE in installed) {
                stringResource(Res.string.settings_sandbox_distro_alpine_installed)
            } else {
                stringResource(Res.string.settings_sandbox_distro_alpine_detail)
            },
            selected = selected == LinuxDistro.ALPINE,
            onSelect = onSelect,
        )
    }
}

@Composable
private fun DistroOption(
    distro: LinuxDistro,
    title: String,
    detail: String,
    selected: Boolean,
    onSelect: (LinuxDistro) -> Unit,
) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .selectable(
                selected = selected,
                role = Role.RadioButton,
                onClick = { onSelect(distro) },
            )
            .handCursor(),
        verticalAlignment = CenterVertically,
    ) {
        RadioButton(selected = selected, onClick = null)
        Column(Modifier.weight(1f)) {
            Text(
                text = title,
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onBackground,
            )
            Text(
                text = detail,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}
