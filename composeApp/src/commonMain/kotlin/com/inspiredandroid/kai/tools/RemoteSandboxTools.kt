package com.inspiredandroid.kai.tools

import com.inspiredandroid.kai.SandboxController
import com.inspiredandroid.kai.SandboxSessions
import com.inspiredandroid.kai.data.currentConversationIdOrNull
import com.inspiredandroid.kai.linux.LinuxDistro
import com.inspiredandroid.kai.network.tools.ParameterSchema
import com.inspiredandroid.kai.network.tools.Tool
import com.inspiredandroid.kai.network.tools.ToolInfo
import com.inspiredandroid.kai.network.tools.ToolSchema
import com.inspiredandroid.kai.remote.RemoteSandboxController
import kai.composeapp.generated.resources.Res
import kai.composeapp.generated.resources.tool_execute_shell_command_description
import kai.composeapp.generated.resources.tool_execute_shell_command_name
import kotlin.time.Duration
import kotlin.time.Duration.Companion.seconds

private const val MAX_TIMEOUT_SECONDS = 120L

/**
 * Shell + process tools for the remote Debian sandbox. Used on iOS (and any
 * other target that wires [RemoteSandboxController] instead of local proot).
 */
object RemoteSandboxShellTool : Tool {
    private lateinit var controller: RemoteSandboxController

    fun bind(controller: SandboxController) {
        this.controller = controller as RemoteSandboxController
    }

    override val timeout: Duration = (MAX_TIMEOUT_SECONDS + 15).seconds

    override val schema: ToolSchema
        get() = ToolSchema(
            name = "execute_shell_command",
            description = """Execute a shell command in the remote Debian 12 sandbox (full root/sudo).
Package manager: apt-get. Working directory and exported env persist per conversation session.
Output is limited; pipe through head/tail/grep for large results.
Default timeout 30s, max ${MAX_TIMEOUT_SECONDS}s.
Set background=true for long-lived jobs and use manage_process to check them.
Set fresh=true for a one-shot shell that does not share session state.""",
            parameters = mapOf(
                "command" to ParameterSchema("string", "The shell command to execute", true),
                "timeout" to ParameterSchema("integer", "Timeout in seconds (default 30, max $MAX_TIMEOUT_SECONDS)", false),
                "working_dir" to ParameterSchema("string", "Directory to cd into before the command (persists in the session)", false),
                "env" to ParameterSchema("object", "Per-command environment overrides (do not persist)", false),
                "background" to ParameterSchema("boolean", "Run as a background job; returns session_id", false),
                "fresh" to ParameterSchema("boolean", "One-shot isolated shell", false),
            ),
        )

    @Suppress("UNCHECKED_CAST")
    override suspend fun execute(args: Map<String, Any>): Any {
        if (!::controller.isInitialized || !controller.status.value.ready) {
            return mapOf("success" to false, "error" to "Remote sandbox is not connected. Open Settings > Sandbox and tap Connect.")
        }
        val command = args["command"] as? String
            ?: return mapOf("success" to false, "error" to "Command is required")
        val timeoutSeconds = ((args["timeout"] as? Number)?.toLong() ?: 30L)
            .coerceIn(1, MAX_TIMEOUT_SECONDS)
        val workingDir = args["working_dir"] as? String
        val envMap = (args["env"] as? Map<String, Any>)
            ?.mapValues { it.value.toString() }
            ?: emptyMap()
        val background = args["background"] as? Boolean ?: false
        if (background) {
            return controller.startBackground(command, workingDir ?: "/root", envMap)
        }
        val fresh = args["fresh"] as? Boolean ?: false
        val sessionId = currentConversationIdOrNull() ?: SandboxSessions.DEFAULT
        val prefix = buildString {
            if (workingDir != null) append("cd ").append(shellSingleQuote(workingDir)).append(" && ")
            envMap.forEach { (k, v) ->
                append(shellSingleQuote(k)).append('=').append(shellSingleQuote(v)).append(' ')
            }
        }
        val wrapped = if (prefix.isEmpty()) command else "$prefix$command"
        return if (fresh) {
            // Route through a throwaway session id so state does not stick.
            val output = controller.executeCommand(wrapped, sessionId = "fresh-${sessionId}-${timeoutSeconds}")
            mapOf("success" to true, "output" to output, "fresh" to true)
        } else {
            val output = controller.executeCommand(wrapped, sessionId = sessionId)
            mapOf("success" to true, "output" to output)
        }
    }

    private fun shellSingleQuote(value: String): String = "'" + value.replace("'", "'\\''") + "'"

    val toolInfo = ToolInfo(
        id = "execute_shell_command",
        name = "Execute Shell Command",
        description = "Execute a shell command in the remote Linux sandbox",
        nameRes = Res.string.tool_execute_shell_command_name,
        descriptionRes = Res.string.tool_execute_shell_command_description,
        isEnabled = false,
        userToggleable = false,
    )
}

object RemoteSandboxProcessTool : Tool {
    private lateinit var controller: RemoteSandboxController

    fun bind(controller: SandboxController) {
        this.controller = controller as RemoteSandboxController
    }

    override val timeout = 15.seconds

    override val schema = ToolSchema(
        name = "manage_process",
        description = """Manage background shell processes started with execute_shell_command (background=true).
Actions:
- list: Show all running and finished background processes
- log: Get output from a process (params: session_id, offset, limit)
- kill: Terminate a running process (params: session_id)
- remove: Remove a finished process from the list (params: session_id)""",
        parameters = mapOf(
            "action" to ParameterSchema("string", "Action to perform: list, log, kill, or remove", true),
            "session_id" to ParameterSchema("string", "Session ID of the process (required for log, kill, remove)", false),
            "offset" to ParameterSchema("integer", "Line offset for log output (default: 0)", false),
            "limit" to ParameterSchema("integer", "Max lines to return for log (default: 200)", false),
        ),
    )

    override suspend fun execute(args: Map<String, Any>): Any {
        if (!::controller.isInitialized || !controller.status.value.ready) {
            return mapOf("success" to false, "error" to "Remote sandbox is not connected")
        }
        val action = args["action"] as? String
            ?: return mapOf("success" to false, "error" to "Action is required")
        return when (action) {
            "list" -> controller.listBackground()
            "log" -> {
                val sessionId = args["session_id"] as? String
                    ?: return mapOf("success" to false, "error" to "session_id is required for log")
                val offset = (args["offset"] as? Number)?.toInt() ?: 0
                val limit = (args["limit"] as? Number)?.toInt() ?: 200
                controller.logBackground(sessionId, offset, limit)
            }
            "kill" -> {
                val sessionId = args["session_id"] as? String
                    ?: return mapOf("success" to false, "error" to "session_id is required for kill")
                controller.killBackground(sessionId)
            }
            "remove" -> {
                val sessionId = args["session_id"] as? String
                    ?: return mapOf("success" to false, "error" to "session_id is required for remove")
                controller.removeBackground(sessionId)
            }
            else -> mapOf("success" to false, "error" to "Unknown action: $action")
        }
    }

    val toolInfo = ToolInfo(
        id = "manage_process",
        name = "Manage Process",
        description = "Manage background shell processes",
        nameRes = null,
        descriptionRes = null,
        isEnabled = false,
        userToggleable = false,
    )
}

/** Distro badge for remote settings UI. */
val RemoteSandboxDistro: LinuxDistro = LinuxDistro.DEBIAN
