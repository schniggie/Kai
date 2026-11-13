package com.inspiredandroid.kai.remote

import com.inspiredandroid.kai.httpClient
import io.ktor.client.call.body
import io.ktor.client.plugins.HttpTimeout
import io.ktor.client.plugins.contentnegotiation.ContentNegotiation
import io.ktor.client.request.bearerAuth
import io.ktor.client.request.delete
import io.ktor.client.request.get
import io.ktor.client.request.parameter
import io.ktor.client.request.post
import io.ktor.client.request.put
import io.ktor.client.request.setBody
import io.ktor.client.statement.bodyAsText
import io.ktor.http.ContentType
import io.ktor.http.contentType
import io.ktor.http.isSuccess
import io.ktor.serialization.kotlinx.json.json
import kotlinx.serialization.json.Json

class RemoteSandboxApi(
    private val baseUrlProvider: () -> String,
    private val tokenProvider: () -> String,
) {
    private val json = Json { ignoreUnknownKeys = true; isLenient = true }

    private val client = httpClient {
        expectSuccess = false
        install(ContentNegotiation) { json(json) }
        install(HttpTimeout) {
            requestTimeoutMillis = 320_000
            connectTimeoutMillis = 30_000
            socketTimeoutMillis = 320_000
        }
    }

    private fun root(): String = baseUrlProvider().trim().trimEnd('/')

    private fun auth(): String = tokenProvider().trim()

    private fun requireConfigured() {
        require(root().isNotBlank()) { "Remote sandbox URL is not set" }
        require(auth().isNotBlank()) { "Remote sandbox token is not set" }
    }

    suspend fun acquire(deviceId: String): RemoteSandboxStatusDto {
        requireConfigured()
        val response = client.post("${root()}/v1/sandboxes/acquire") {
            bearerAuth(auth())
            contentType(ContentType.Application.Json)
            setBody(RemoteAcquireRequest(deviceId))
        }
        if (!response.status.isSuccess()) {
            error("acquire failed (${response.status}): ${response.bodyAsText()}")
        }
        return response.body()
    }

    suspend fun status(deviceId: String): RemoteSandboxStatusDto {
        requireConfigured()
        val response = client.get("${root()}/v1/sandboxes/$deviceId") {
            bearerAuth(auth())
        }
        if (!response.status.isSuccess()) {
            error("status failed (${response.status}): ${response.bodyAsText()}")
        }
        return response.body()
    }

    suspend fun destroy(deviceId: String, wipe: Boolean) {
        requireConfigured()
        val response = client.delete("${root()}/v1/sandboxes/$deviceId") {
            bearerAuth(auth())
            parameter("wipe", wipe)
        }
        if (!response.status.isSuccess()) {
            error("destroy failed (${response.status}): ${response.bodyAsText()}")
        }
    }

    suspend fun exec(deviceId: String, request: RemoteExecRequest): RemoteExecResponse {
        requireConfigured()
        val response = client.post("${root()}/v1/sandboxes/$deviceId/exec") {
            bearerAuth(auth())
            contentType(ContentType.Application.Json)
            setBody(request)
        }
        if (!response.status.isSuccess()) {
            error("exec failed (${response.status}): ${response.bodyAsText()}")
        }
        return response.body()
    }

    suspend fun closeSession(deviceId: String, sessionId: String) {
        requireConfigured()
        client.post("${root()}/v1/sandboxes/$deviceId/sessions/$sessionId/close") {
            bearerAuth(auth())
        }
    }

    suspend fun startJob(deviceId: String, request: RemoteJobStartRequest): RemoteJobResponse {
        requireConfigured()
        val response = client.post("${root()}/v1/sandboxes/$deviceId/jobs") {
            bearerAuth(auth())
            contentType(ContentType.Application.Json)
            setBody(request)
        }
        if (!response.status.isSuccess()) {
            error("start job failed (${response.status}): ${response.bodyAsText()}")
        }
        return response.body()
    }

    suspend fun listJobs(deviceId: String): List<RemoteJobResponse> {
        requireConfigured()
        val response = client.get("${root()}/v1/sandboxes/$deviceId/jobs") {
            bearerAuth(auth())
        }
        if (!response.status.isSuccess()) {
            error("list jobs failed (${response.status}): ${response.bodyAsText()}")
        }
        return response.body()
    }

    suspend fun getJob(deviceId: String, jobId: String): RemoteJobResponse {
        requireConfigured()
        val response = client.get("${root()}/v1/sandboxes/$deviceId/jobs/$jobId") {
            bearerAuth(auth())
        }
        if (!response.status.isSuccess()) {
            error("get job failed (${response.status}): ${response.bodyAsText()}")
        }
        return response.body()
    }

    suspend fun killJob(deviceId: String, jobId: String): RemoteJobResponse {
        requireConfigured()
        val response = client.post("${root()}/v1/sandboxes/$deviceId/jobs/$jobId/kill") {
            bearerAuth(auth())
        }
        if (!response.status.isSuccess()) {
            error("kill job failed (${response.status}): ${response.bodyAsText()}")
        }
        return response.body()
    }

    suspend fun removeJob(deviceId: String, jobId: String) {
        requireConfigured()
        client.delete("${root()}/v1/sandboxes/$deviceId/jobs/$jobId") {
            bearerAuth(auth())
        }
    }

    suspend fun listFs(deviceId: String, path: String): RemoteFsListResponse {
        requireConfigured()
        val response = client.get("${root()}/v1/sandboxes/$deviceId/fs") {
            bearerAuth(auth())
            parameter("path", path)
        }
        if (!response.status.isSuccess()) {
            error("list fs failed (${response.status}): ${response.bodyAsText()}")
        }
        return response.body()
    }

    suspend fun readFsText(deviceId: String, path: String, maxBytes: Int): String {
        requireConfigured()
        val response = client.get("${root()}/v1/sandboxes/$deviceId/fs/raw") {
            bearerAuth(auth())
            parameter("path", path)
            parameter("max_bytes", maxBytes)
        }
        if (!response.status.isSuccess()) {
            error("read fs failed (${response.status}): ${response.bodyAsText()}")
        }
        return response.bodyAsText()
    }

    suspend fun writeFsText(deviceId: String, path: String, content: String) {
        requireConfigured()
        val response = client.put("${root()}/v1/sandboxes/$deviceId/fs/text") {
            bearerAuth(auth())
            contentType(ContentType.Application.Json)
            setBody(RemoteFsWriteRequest(path = path, content = content))
        }
        if (!response.status.isSuccess()) {
            error("write fs failed (${response.status}): ${response.bodyAsText()}")
        }
    }

    suspend fun deleteFs(deviceId: String, path: String, recursive: Boolean) {
        requireConfigured()
        val response = client.delete("${root()}/v1/sandboxes/$deviceId/fs") {
            bearerAuth(auth())
            parameter("path", path)
            parameter("recursive", recursive)
        }
        if (!response.status.isSuccess()) {
            error("delete fs failed (${response.status}): ${response.bodyAsText()}")
        }
    }

    suspend fun renameFs(deviceId: String, path: String, newName: String): String {
        requireConfigured()
        val response = client.post("${root()}/v1/sandboxes/$deviceId/fs/rename") {
            bearerAuth(auth())
            parameter("path", path)
            parameter("new_name", newName)
        }
        if (!response.status.isSuccess()) {
            error("rename fs failed (${response.status}): ${response.bodyAsText()}")
        }
        return response.body<RemoteRenameResponse>().path
    }
}
