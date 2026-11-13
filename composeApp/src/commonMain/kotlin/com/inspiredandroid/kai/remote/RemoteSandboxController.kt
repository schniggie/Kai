package com.inspiredandroid.kai.remote

import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.snapshots.SnapshotStateList
import com.inspiredandroid.kai.CommandHandle
import com.inspiredandroid.kai.NoOpCommandHandle
import com.inspiredandroid.kai.SandboxController
import com.inspiredandroid.kai.SandboxFileEntry
import com.inspiredandroid.kai.SandboxSessions
import com.inspiredandroid.kai.SandboxStatus
import com.inspiredandroid.kai.SandboxStatusLabel
import com.inspiredandroid.kai.TerminalLine
import com.inspiredandroid.kai.TextFileResult
import com.inspiredandroid.kai.data.AppSettings
import com.inspiredandroid.kai.linux.LinuxDistro
import com.inspiredandroid.kai.openUrl
import com.inspiredandroid.kai.saveFileToDevice
import io.github.vinceglb.filekit.PlatformFile
import io.github.vinceglb.filekit.name
import io.github.vinceglb.filekit.readBytes
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext

/**
 * Sandbox backed by the remote Docker orchestrator — one container per
 * (token, device_id). Used on iOS (and optionally elsewhere) instead of proot.
 */
class RemoteSandboxController(
    private val appSettings: AppSettings,
    private val api: RemoteSandboxApi = RemoteSandboxApi(
        baseUrlProvider = { appSettings.getRemoteSandboxUrl() },
        tokenProvider = { appSettings.getRemoteSandboxToken() },
    ),
) : SandboxController {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private val _status = MutableStateFlow(initialStatus())
    override val status: StateFlow<SandboxStatus> = _status

    private val _sessions = MutableStateFlow<List<String>>(emptyList())
    override val sessions: StateFlow<List<String>> = _sessions

    private val transcripts = mutableMapOf<String, SnapshotStateList<TerminalLine>>()
    private val transcriptsLock = Mutex()
    private var setupJob: kotlinx.coroutines.Job? = null

    init {
        // Reflect saved credentials immediately so Settings can show Connect vs Ready.
        refreshFromSettings()
        if (appSettings.isRemoteSandboxConfigured() && appSettings.isSandboxEnabled()) {
            scope.launch { runCatching { refreshStatus() } }
        }
    }

    fun refreshFromSettings() {
        val configured = appSettings.isRemoteSandboxConfigured()
        _status.update { current ->
            if (current.ready || current.working) current
            else SandboxStatus(
                installed = configured,
                ready = false,
                working = false,
                label = if (configured) SandboxStatusLabel.NotInstalled else SandboxStatusLabel.NotInstalled,
                distro = LinuxDistro.DEBIAN,
                installedDistros = if (configured) setOf(LinuxDistro.DEBIAN) else emptySet(),
            )
        }
    }

    override fun setup() {
        if (!appSettings.isRemoteSandboxConfigured()) {
            _status.value = SandboxStatus(
                error = true,
                label = SandboxStatusLabel.Failure.Setup("Set URL and token first"),
                distro = LinuxDistro.DEBIAN,
            )
            return
        }
        setupJob?.cancel()
        setupJob = scope.launch {
            _status.value = SandboxStatus(
                installed = true,
                working = true,
                progress = 0.2f,
                label = SandboxStatusLabel.Downloading,
                distro = LinuxDistro.DEBIAN,
                installedDistros = setOf(LinuxDistro.DEBIAN),
            )
            try {
                val info = api.acquire(appSettings.getOrCreateRemoteSandboxDeviceId())
                _status.value = statusFrom(info, packagesInstalled = true)
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                _status.value = SandboxStatus(
                    installed = true,
                    error = true,
                    label = SandboxStatusLabel.Failure.Setup(e.message ?: e::class.simpleName.orEmpty()),
                    distro = LinuxDistro.DEBIAN,
                    installedDistros = setOf(LinuxDistro.DEBIAN),
                )
            }
        }
    }

    override fun cancel() {
        setupJob?.cancel()
        setupJob = null
        _status.update {
            it.copy(working = false, progress = null, label = SandboxStatusLabel.NotInstalled)
        }
    }

    override fun reset() {
        scope.launch {
            _status.update { it.copy(working = true, label = SandboxStatusLabel.Installing) }
            try {
                api.destroy(appSettings.getOrCreateRemoteSandboxDeviceId(), wipe = true)
            } catch (_: Exception) {
            }
            transcripts.clear()
            _sessions.value = emptyList()
            _status.value = SandboxStatus(
                installed = appSettings.isRemoteSandboxConfigured(),
                ready = false,
                distro = LinuxDistro.DEBIAN,
                installedDistros = if (appSettings.isRemoteSandboxConfigured()) setOf(LinuxDistro.DEBIAN) else emptySet(),
                label = SandboxStatusLabel.NotInstalled,
            )
        }
    }

    override fun installPackages() {
        scope.launch {
            val pkgs = LinuxDistro.DEBIAN.optionalPackages.joinToString(" ")
            _status.update {
                it.copy(working = true, label = SandboxStatusLabel.InstallingPackage("optional packages"))
            }
            try {
                val result = api.exec(
                    appSettings.getOrCreateRemoteSandboxDeviceId(),
                    RemoteExecRequest(
                        command = "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y $pkgs",
                        sessionId = SandboxSessions.SYSTEM,
                        timeout = 300,
                        fresh = true,
                    ),
                )
                if (result.exitCode != 0) {
                    _status.update {
                        it.copy(
                            working = false,
                            error = true,
                            label = SandboxStatusLabel.Failure.Package(
                                "optional packages",
                                result.stderr.ifBlank { result.stdout }.take(500),
                            ),
                        )
                    }
                } else {
                    refreshStatus(packagesInstalled = true)
                }
            } catch (e: Exception) {
                _status.update {
                    it.copy(
                        working = false,
                        error = true,
                        label = SandboxStatusLabel.Failure.Package("optional packages", e.message.orEmpty()),
                    )
                }
            }
        }
    }

    override suspend fun executeCommand(command: String, sessionId: String): String =
        withContext(Dispatchers.Default) {
            ensureReady()
            val result = api.exec(
                appSettings.getOrCreateRemoteSandboxDeviceId(),
                RemoteExecRequest(command = command, sessionId = sessionId, timeout = 60),
            )
            appendTranscript(sessionId, TerminalLine.Command(command))
            if (result.stdout.isNotBlank()) appendTranscript(sessionId, TerminalLine.Output(result.stdout))
            if (result.stderr.isNotBlank()) appendTranscript(sessionId, TerminalLine.Error(result.stderr))
            trackSession(sessionId)
            buildString {
                append(result.stdout)
                if (result.stderr.isNotBlank()) {
                    if (isNotEmpty()) append('\n')
                    append(result.stderr)
                }
                if (isNotEmpty()) append('\n')
                append("exit_code=${result.exitCode}")
            }
        }

    override suspend fun executeCommandStreaming(
        command: String,
        onStdout: (String) -> Unit,
        onStderr: (String) -> Unit,
        sessionId: String,
    ): CommandHandle = withContext(Dispatchers.Default) {
        ensureReady()
        var cancelled = false
        appendTranscript(sessionId, TerminalLine.Command(command))
        trackSession(sessionId)
        try {
            val result = api.exec(
                appSettings.getOrCreateRemoteSandboxDeviceId(),
                RemoteExecRequest(command = command, sessionId = sessionId, timeout = 120),
            )
            if (!cancelled) {
                if (result.stdout.isNotEmpty()) {
                    onStdout(result.stdout)
                    appendTranscript(sessionId, TerminalLine.Output(result.stdout))
                }
                if (result.stderr.isNotEmpty()) {
                    onStderr(result.stderr)
                    appendTranscript(sessionId, TerminalLine.Error(result.stderr))
                }
            }
            object : CommandHandle {
                override fun cancel() {
                    cancelled = true
                }

                override fun isCancelled(): Boolean = cancelled
                override suspend fun writeInput(line: String) {}
                override suspend fun awaitExit(): Int = result.exitCode
            }
        } catch (e: Exception) {
            onStderr(e.message ?: "remote exec failed")
            appendTranscript(sessionId, TerminalLine.Error(e.message ?: "remote exec failed"))
            NoOpCommandHandle
        }
    }

    override fun closeSession(sessionId: String) {
        scope.launch {
            runCatching {
                api.closeSession(appSettings.getOrCreateRemoteSandboxDeviceId(), sessionId)
            }
            _sessions.update { it - sessionId }
        }
    }

    override fun transcriptFor(sessionId: String): SnapshotStateList<TerminalLine> {
        return transcripts.getOrPut(sessionId) { mutableStateListOf() }
    }

    override fun clearTranscript(sessionId: String) {
        transcripts[sessionId]?.clear()
    }

    override suspend fun listDirectory(path: String): List<SandboxFileEntry> = withContext(Dispatchers.Default) {
        ensureReady()
        api.listFs(appSettings.getOrCreateRemoteSandboxDeviceId(), path).entries.map {
            SandboxFileEntry(
                name = it.name,
                path = it.path,
                isDirectory = it.isDirectory,
                sizeBytes = it.sizeBytes,
                lastModifiedMs = it.lastModifiedMs,
            )
        }
    }

    override suspend fun readTextFile(path: String, maxBytes: Int, force: Boolean): TextFileResult =
        withContext(Dispatchers.Default) {
            ensureReady()
            try {
                val text = api.readFsText(appSettings.getOrCreateRemoteSandboxDeviceId(), path, maxBytes + 1)
                if (text.length > maxBytes) {
                    // Approximate size when the API truncated by byte count differently.
                    TextFileResult.TooLarge(sizeBytes = text.length.toLong())
                } else if (!force && text.any { it == '\u0000' }) {
                    TextFileResult.Binary
                } else {
                    TextFileResult.Text(text, editable = true)
                }
            } catch (_: Exception) {
                TextFileResult.Unreadable
            }
        }

    override suspend fun writeTextFile(path: String, content: String): Boolean = withContext(Dispatchers.Default) {
        ensureReady()
        try {
            api.writeFsText(appSettings.getOrCreateRemoteSandboxDeviceId(), path, content)
            true
        } catch (_: Exception) {
            false
        }
    }

    override suspend fun openFile(path: String): Result<Unit> = withContext(Dispatchers.Default) {
        ensureReady()
        try {
            val text = api.readFsText(appSettings.getOrCreateRemoteSandboxDeviceId(), path, 2_000_000)
            val name = path.substringAfterLast('/')
            val base = name.substringBeforeLast('.', name)
            val ext = name.substringAfterLast('.', "txt")
            saveFileToDevice(text.encodeToByteArray(), base, ext)
            Result.success(Unit)
        } catch (e: Exception) {
            // Fall back to opening http(s) paths if the agent linked a URL.
            if (path.startsWith("http://") || path.startsWith("https://")) {
                Result.success(Unit).also { openUrl(path) }
            } else {
                Result.failure(e)
            }
        }
    }

    override suspend fun deleteEntry(path: String, recursive: Boolean): Boolean = withContext(Dispatchers.Default) {
        ensureReady()
        try {
            api.deleteFs(appSettings.getOrCreateRemoteSandboxDeviceId(), path, recursive)
            true
        } catch (_: Exception) {
            false
        }
    }

    override suspend fun renameEntry(path: String, newName: String): Result<String> =
        withContext(Dispatchers.Default) {
            ensureReady()
            try {
                Result.success(api.renameFs(appSettings.getOrCreateRemoteSandboxDeviceId(), path, newName))
            } catch (e: Exception) {
                val msg = e.message.orEmpty()
                if (msg.contains("collision", ignoreCase = true)) Result.failure(IllegalStateException("collision"))
                else Result.failure(e)
            }
        }

    override suspend fun importFile(directoryPath: String, source: PlatformFile): Result<String> =
        withContext(Dispatchers.Default) {
            ensureReady()
            try {
                val bytes = source.readBytes()
                val original = source.name
                var name = original
                var i = 1
                val listing = api.listFs(appSettings.getOrCreateRemoteSandboxDeviceId(), directoryPath)
                while (listing.entries.any { it.name == name }) {
                    val dot = original.lastIndexOf('.')
                    name = if (dot > 0) {
                        "${original.substring(0, dot)}-$i${original.substring(dot)}"
                    } else {
                        "$original-$i"
                    }
                    i++
                }
                val dest = directoryPath.trimEnd('/') + "/" + name
                val b64 = bytes.toBase64()
                val result = api.exec(
                    appSettings.getOrCreateRemoteSandboxDeviceId(),
                    RemoteExecRequest(
                        command = "python3 -c \"import base64,sys; open(sys.argv[1],'wb').write(base64.b64decode(sys.argv[2]))\" ${shellQuote(dest)} ${shellQuote(b64)}",
                        sessionId = SandboxSessions.SYSTEM,
                        timeout = 120,
                        fresh = true,
                    ),
                )
                if (result.exitCode != 0) {
                    Result.failure(IllegalStateException(result.stderr.ifBlank { result.stdout }))
                } else {
                    Result.success(dest)
                }
            } catch (e: Exception) {
                Result.failure(e)
            }
        }

    suspend fun startBackground(command: String, workingDir: String?, env: Map<String, String>): Map<String, Any> {
        ensureReady()
        val job = api.startJob(
            appSettings.getOrCreateRemoteSandboxDeviceId(),
            RemoteJobStartRequest(command = command, workingDir = workingDir, env = env.ifEmpty { null }),
        )
        return mapOf(
            "success" to true,
            "session_id" to job.jobId,
            "status" to job.status,
        )
    }

    suspend fun listBackground(): Map<String, Any> {
        ensureReady()
        val jobs = api.listJobs(appSettings.getOrCreateRemoteSandboxDeviceId())
        return mapOf(
            "success" to true,
            "processes" to jobs.map {
                mapOf(
                    "session_id" to it.jobId,
                    "status" to it.status,
                    "command" to it.command,
                    "exit_code" to it.exitCode,
                )
            },
        )
    }

    suspend fun logBackground(jobId: String, offset: Int, limit: Int): Map<String, Any> {
        ensureReady()
        val job = api.getJob(appSettings.getOrCreateRemoteSandboxDeviceId(), jobId)
        val lines = (job.stdout + "\n" + job.stderr).lines()
        val slice = lines.drop(offset).take(limit)
        return mapOf(
            "success" to true,
            "session_id" to job.jobId,
            "status" to job.status,
            "exit_code" to job.exitCode,
            "lines" to slice,
        )
    }

    suspend fun killBackground(jobId: String): Map<String, Any> {
        ensureReady()
        val job = api.killJob(appSettings.getOrCreateRemoteSandboxDeviceId(), jobId)
        return mapOf("success" to true, "session_id" to job.jobId, "status" to job.status)
    }

    suspend fun removeBackground(jobId: String): Map<String, Any> {
        ensureReady()
        api.removeJob(appSettings.getOrCreateRemoteSandboxDeviceId(), jobId)
        return mapOf("success" to true)
    }

    private suspend fun refreshStatus(packagesInstalled: Boolean = true) {
        val info = api.status(appSettings.getOrCreateRemoteSandboxDeviceId())
        _status.value = statusFrom(info, packagesInstalled)
    }

    private fun statusFrom(info: RemoteSandboxStatusDto, packagesInstalled: Boolean): SandboxStatus =
        SandboxStatus(
            installed = true,
            ready = info.ready,
            working = false,
            progress = null,
            label = if (info.ready) SandboxStatusLabel.Ready else SandboxStatusLabel.Configuring,
            diskUsageMB = (info.diskUsageBytes / (1024L * 1024L)).coerceAtLeast(0),
            packagesInstalled = packagesInstalled,
            error = false,
            distro = LinuxDistro.DEBIAN,
            installedDistros = setOf(LinuxDistro.DEBIAN),
        )

    private fun initialStatus(): SandboxStatus {
        val configured = appSettings.isRemoteSandboxConfigured()
        return SandboxStatus(
            installed = configured,
            ready = false,
            label = SandboxStatusLabel.NotInstalled,
            distro = LinuxDistro.DEBIAN,
            installedDistros = if (configured) setOf(LinuxDistro.DEBIAN) else emptySet(),
        )
    }

    private suspend fun ensureReady() {
        if (_status.value.ready) return
        val info = api.acquire(appSettings.getOrCreateRemoteSandboxDeviceId())
        _status.value = statusFrom(info, packagesInstalled = true)
        if (!_status.value.ready) error("Remote sandbox is not ready")
    }

    private fun trackSession(sessionId: String) {
        _sessions.update { current ->
            if (sessionId in current) current else current + sessionId
        }
    }

    private suspend fun appendTranscript(sessionId: String, line: TerminalLine) {
        transcriptsLock.withLock {
            val list = transcripts.getOrPut(sessionId) { mutableStateListOf() }
            list.add(line)
            while (list.size > 500) list.removeAt(0)
        }
    }

    private fun shellQuote(value: String): String = "'" + value.replace("'", "'\\''") + "'"

    private fun ByteArray.toBase64(): String {
        val table = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
        val out = StringBuilder((size + 2) / 3 * 4)
        var i = 0
        while (i < size) {
            val b0 = this[i].toInt() and 0xff
            val b1 = if (i + 1 < size) this[i + 1].toInt() and 0xff else 0
            val b2 = if (i + 2 < size) this[i + 2].toInt() and 0xff else 0
            out.append(table[b0 shr 2])
            out.append(table[((b0 and 0x3) shl 4) or (b1 shr 4)])
            out.append(if (i + 1 < size) table[((b1 and 0xf) shl 2) or (b2 shr 6)] else '=')
            out.append(if (i + 2 < size) table[b2 and 0x3f] else '=')
            i += 3
        }
        return out.toString()
    }
}
