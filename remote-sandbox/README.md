# Kai Remote Sandbox

Small Docker Compose stack that gives each Kai user/device a **private Debian container with full root/sudo**. Built for a handful of trusted people — especially iOS, where an on-device Linux sandbox is not practical.

## Model

| Concept | Behavior |
| --- | --- |
| Auth | Bearer token from `tokens.txt` (`token_id` + `secret`) |
| Isolation key | `(token_id, device_id)` → one container + one named volume |
| Image | Debian 12 + common tools; runs as root |
| Persistence | Named volume mounted at `/root` (home, skills, projects, SSH keys). Container stop keeps apt packages; container destroy does not |
| Idle | Container stopped after `IDLE_TIMEOUT_SECONDS` (default 1h); volume kept |
| Destroy | `DELETE` stops/removes container; `?wipe=true` also deletes the volume |

> Note: only `/root` is on the named volume. System packages installed with `apt` live in the container writable layer and survive stop/start, but are lost if the container is destroyed without recreate-from-image. For durable tooling, prefer installs under `/root/.local` or re-run apt after wipe.

## Quick start

```bash
cd remote-sandbox

# 1. Tokens
cp tokens.txt.example tokens.txt
# edit secrets — one line per person:
#   alice  <secret>  Alice's phone

# 2. Build sandbox image + orchestrator
docker compose build
docker compose --profile build build sandbox-image
# or simply:
docker build -t kai-sandbox:local ./sandbox-image
docker compose up -d orchestrator

# 3. Smoke test
curl -s localhost:8080/healthz
curl -s -X POST localhost:8080/v1/sandboxes/acquire \
  -H "Authorization: Bearer <secret>" \
  -H "Content-Type: application/json" \
  -d '{"device_id":"iphone-chris"}'
curl -s -X POST localhost:8080/v1/sandboxes/iphone-chris/exec \
  -H "Authorization: Bearer <secret>" \
  -H "Content-Type: application/json" \
  -d '{"command":"uname -a && whoami && sudo id","session_id":"demo"}'
```

Put a reverse proxy with TLS in front for phones on the internet (Caddy / Traefik / nginx). Do **not** expose the Docker socket beyond the orchestrator container.

## Kai app settings

In Kai → Settings → Sandbox (iOS / remote mode):

- **URL** — `https://sandbox.example.com` (no trailing path)
- **Token** — the secret from `tokens.txt`
- **Device ID** — stable per phone (auto-generated once; editable)

Kai then acquires that device’s container and routes `execute_shell_command`, the Terminal tab, and the Files tab through this API.

## API (v1)

All routes except `/healthz` require `Authorization: Bearer <secret>`.

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/v1/sandboxes/acquire` | Create/start this device’s container |
| `GET` | `/v1/sandboxes/{device_id}` | Status |
| `DELETE` | `/v1/sandboxes/{device_id}?wipe=` | Destroy (`wipe=true` drops volume) |
| `POST` | `/v1/sandboxes/{device_id}/exec` | Run command (persistent session by default) |
| `POST` | `/v1/sandboxes/{device_id}/sessions/{id}/close` | Drop a bash session |
| `POST/GET/DELETE` | `/v1/sandboxes/{device_id}/jobs…` | Background jobs |
| `GET/PUT/DELETE` | `/v1/sandboxes/{device_id}/fs…` | File browser |

`POST …/exec` body:

```json
{
  "command": "apt-get update && apt-get install -y ffmpeg",
  "session_id": "conversation-or-terminal-id",
  "timeout": 60,
  "working_dir": "/root",
  "env": {"FOO": "bar"},
  "fresh": false
}
```

Persistent sessions keep cwd/env across calls (per `session_id`). `fresh: true` runs a one-shot bash instead.

## Security (trusted group)

- Full root inside the container is intentional.
- Cap CPU/memory on containers (defaults: 2 CPU, 2 GB).
- Orchestrator caps: ≤16 containers host-wide and ≤4 per token; idle stop **removes** the container (volume kept) so slots recycle; FS read/write ≤ 5 MiB (stream-capped, JSON included, Docker I/O off the event loop); listings ≤ 2000 entries; ≤ 4 active jobs per sandbox; truncated output; wall-clock deadlines that SIGKILL lingering execs; finished jobs expire after 1h.
- Keep the orchestrator on a private network / VPN when possible.
- Rotate tokens by editing `tokens.txt` (hot-reloaded on next auth miss).
- Assume a compromised token can attack other containers on `kai_sandbox_net` and the host’s reachable network — isolate accordingly.
