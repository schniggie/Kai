package com.inspiredandroid.kai

import com.inspiredandroid.kai.data.AppSettings
import com.inspiredandroid.kai.remote.RemoteSandboxController
import com.inspiredandroid.kai.tools.RemoteSandboxProcessTool
import com.inspiredandroid.kai.tools.RemoteSandboxShellTool
import org.koin.core.component.KoinComponent
import org.koin.core.component.inject

private object IosSandboxKoin : KoinComponent {
    val appSettings: AppSettings by inject()
}

actual fun createSandboxController(): SandboxController {
    val controller = RemoteSandboxController(IosSandboxKoin.appSettings)
    RemoteSandboxShellTool.bind(controller)
    RemoteSandboxProcessTool.bind(controller)
    return controller
}
