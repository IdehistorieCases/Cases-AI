# Windows Codex CLI bridge

Run this bridge on the signed-in Windows host. It defaults to 127.0.0.1:18765. If a legacy bridge already owns that port, choose a separate loopback port by setting both `LOCALAI_CODEX_BRIDGE_PORT` on the bridge and `LUNA_BRIDGE_PORT` in the gateway `.env`; use 18767 to preserve the old listener on 18765. The bridge accepts only loopback or private, non-public IP bind addresses. It invokes authenticated Codex CLI in ephemeral, read-only temporary directories with high reasoning effort. Requests and access logs are not written to disk.

Set a new bridge bearer token in the current PowerShell session without echoing it:

    $secure = Read-Host "Bridge token (24+ random characters)" -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { $env:LOCALAI_CODEX_BRIDGE_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
    # Keep the listener on loopback; Docker Desktop forwards host.docker.internal to it.
    $env:LOCALAI_CODEX_BRIDGE_HOST = "127.0.0.1"
    $env:LOCALAI_CODEX_BRIDGE_PORT = "18767"
    python integrations/codex-bridge/codex_bridge.py

The signed-in Codex CLI must already be authenticated for this Windows user. Keep this process running while Luna is enabled. Verify the chosen listener is bound only to `127.0.0.1` and that `host.docker.internal:<port>` is reachable from the gateway container. Do not bind to a public or ordinary LAN interface. If Docker Desktop does not forward the loopback port from containers on this host, leave Luna disabled until a private host route is configured safely.

The bridge requires bearer auth for models and completions. It never accepts a caller-supplied URL or shell command. Request-line, headers, and body reads share a 10-second total deadline; completion processing starts after that read deadline is canceled. The loopback bind keeps the listener off LAN interfaces; Docker Desktop forwards the host-gateway route and the gateway authenticates with its separate bridge token.

Stop the foreground bridge with `Ctrl+C`. Confirm that the loopback listener is gone before starting it again:

```powershell
Get-NetTCPConnection -LocalAddress $env:LOCALAI_CODEX_BRIDGE_HOST -LocalPort $env:LOCALAI_CODEX_BRIDGE_PORT -State Listen -ErrorAction SilentlyContinue
```

If another process owns the port, identify it before taking action; do not stop an unrelated process to free the port.

## Authentication and isolation limits

This bridge uses the existing Codex CLI sign-in for the Windows user. That account session is not an OpenAI API key and does not make this bridge equivalent to arbitrary API credentials. The CLI runs as that Windows user. Its ephemeral directory, disabled tools, and read-only mode reduce what a completion can change, but they are not a separate operating-system security boundary. Keep the listener on loopback, use an independent bridge token, and send only prompts approved for the cloud provider.
