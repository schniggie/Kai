# Remote Linux Sandbox

**Last verified:** 2026-11-13

Kai can attach to a small self-hosted Docker Compose stack so the assistant gets a **full Debian environment with root/sudo** without running Linux on the phone. Each `(API token, device id)` pair owns one container and one persistent `/root` volume. This is the sandbox path for **iOS**; Android keeps its on-device proot sandbox.

## Concepts

### Why remote

iOS cannot host a proot/Linux userspace the way Android does (no third-party JIT, no arbitrary ELF exec). A remote orchestrator gives the agent the same job — install packages, run scripts, SSH out, manage files — while the phone stays a thin client.

### One container per user/device

The orchestrator authenticates a Bearer token from `remote-sandbox/tokens.txt`. Together with the device id stored in Kai settings, that selects exactly one sandbox. Two phones with the same token but different device ids get two containers. Wiping one does not touch the other.

### What the agent can do

When Connected and the sandbox toggle is on, Kai advertises `execute_shell_command` and `manage_process`. Commands run as root in Debian 12. Persistent bash sessions are keyed by conversation id (same mental model as Android). Optional packages can be installed from Settings; the Packages and Files tabs talk to the same remote home.

### Sideloading the iOS app

This feature is aimed at a private group. Without an Apple Developer Program membership, install via SideStore / AltStore / Xcode with each person’s free Apple ID (7-day refresh, 3-app limit). TestFlight and Ad Hoc need the paid program.

## Behavior

- **Settings → Sandbox (iOS)** shows Orchestrator URL, API token, and Device ID. **Connect** calls `POST /v1/sandboxes/acquire`. **Disconnect & wipe** destroys the container and volume.
- **Chat terminal icon** appears on iOS once the platform supports sandbox; the inline Terminal / Files / Packages surface uses `RemoteSandboxController`.
- **Idle stop**: the orchestrator stops unused containers after `IDLE_TIMEOUT_SECONDS` (default 1 hour) but keeps the volume. The next command starts the container again.
- **Android unchanged**: local proot remains the Android path; this doc does not replace [sandbox.md](sandbox.md).

## Limitations

- Needs network reachability to the orchestrator (TLS recommended).
- Terminal streaming is request/response (output arrives when the command finishes), not a live PTY.
- Apt packages live in the container layer: they survive stop/start, but a wipe recreates a clean image plus the `/root` volume only.
- Full root is intentional for a trusted group — isolate the Docker host accordingly.
- Shared-orchestrator caps apply: 5 MiB file payloads, 2000-entry directory listings, 4 active background jobs per sandbox, truncated command output, wall-clock timeouts on exec, PID limit per container, tmpfs-capped `/tmp`, rotated Docker logs for orchestrator and sandboxes, storage_opt container budget when the graph driver supports it, hard ~2 GiB `/root` volumes backed by sparse ext4 images under `SANDBOX_VOLUME_ROOT`, concurrent docker ops globally and per sandbox/token (FS/status/exec share admission), and global/per-token container+volume caps. `/v1` rejects unauthenticated requests before buffering bodies; `/healthz` is rate-limited.

## Key Files

| File | Purpose |
| --- | --- |
| `remote-sandbox/docker-compose.yml` | Orchestrator service + sandbox image build |
| `remote-sandbox/orchestrator/app/` | FastAPI auth, acquire/exec/jobs/fs, Docker lifecycle |
| `remote-sandbox/sandbox-image/` | Debian image + `kai-sessiond` persistent bash daemon |
| `composeApp/src/commonMain/.../remote/RemoteSandboxController.kt` | KMP client implementing `SandboxController` |
| `composeApp/src/commonMain/.../tools/RemoteSandboxTools.kt` | iOS shell + process tools |
| `composeApp/src/iosMain/.../SandboxController.ios.kt` | Wires remote controller on iOS |
| `composeApp/src/commonMain/.../ui/settings/SandboxSettings.kt` | Remote URL/token/device UI |
