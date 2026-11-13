package com.inspiredandroid.kai.remote

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

@Serializable
data class RemoteAcquireRequest(
    @SerialName("device_id") val deviceId: String,
)

@Serializable
data class RemoteSandboxStatusDto(
    @SerialName("sandbox_id") val sandboxId: String = "",
    @SerialName("device_id") val deviceId: String = "",
    val status: String = "",
    @SerialName("container_name") val containerName: String = "",
    val ready: Boolean = false,
    @SerialName("disk_usage_bytes") val diskUsageBytes: Long = 0,
)

@Serializable
data class RemoteExecRequest(
    val command: String,
    @SerialName("session_id") val sessionId: String = "default",
    val timeout: Int = 30,
    @SerialName("working_dir") val workingDir: String? = null,
    val env: Map<String, String>? = null,
    val fresh: Boolean = false,
)

@Serializable
data class RemoteExecResponse(
    val stdout: String = "",
    val stderr: String = "",
    @SerialName("exit_code") val exitCode: Int = 0,
    @SerialName("timed_out") val timedOut: Boolean = false,
    @SerialName("session_id") val sessionId: String = "default",
)

@Serializable
data class RemoteJobStartRequest(
    val command: String,
    @SerialName("working_dir") val workingDir: String? = null,
    val env: Map<String, String>? = null,
)

@Serializable
data class RemoteJobResponse(
    @SerialName("job_id") val jobId: String = "",
    val status: String = "",
    val command: String = "",
    @SerialName("exit_code") val exitCode: Int? = null,
    val stdout: String = "",
    val stderr: String = "",
)

@Serializable
data class RemoteFsEntryDto(
    val name: String,
    val path: String,
    @SerialName("is_directory") val isDirectory: Boolean,
    @SerialName("size_bytes") val sizeBytes: Long = 0,
    @SerialName("last_modified_ms") val lastModifiedMs: Long = 0,
)

@Serializable
data class RemoteFsListResponse(
    val path: String = "/",
    val entries: List<RemoteFsEntryDto> = emptyList(),
)

@Serializable
data class RemoteFsWriteRequest(
    val path: String,
    val content: String,
    val encoding: String = "utf-8",
)

@Serializable
data class RemoteRenameResponse(
    val path: String = "",
)
