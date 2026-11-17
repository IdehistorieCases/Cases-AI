# Windows Codex CLI bridge

Run this bridge on the signed-in Windows host. It defaults to 127.0.0.1:18765 for local development. When the Docker gateway connects through `host.docker.internal`, bind it to the matching private Docker Desktop host-interface address instead: a container's `host.docker.internal` destination is the host's internal IP, not the host's loopback address. The bridge accepts only loopback or private, non-public IP bind addresses. It invokes authenticated Codex CLI in ephemeral, read-only temporary directories with high reasoning effort. Requests and access logs are not written to disk.

Set a new bridge bearer token in the current PowerShell session without echoing it:

    $secure = Read-Host "Bridge token (24+ random characters)" -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { $env:LOCALAI_CODEX_BRIDGE_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
    # For the Docker gateway only: set to the private IP resolved for host.docker.internal
    # from the gateway container, and verify Windows assigns it to a Docker/WSL adapter.
    $env:LOCALAI_CODEX_BRIDGE_HOST = "<private Docker Desktop host-interface IP>"
    python integrations/codex-bridge/codex_bridge.py

The signed-in Codex CLI must already be authenticated for this Windows user. Keep this process running while Luna is enabled. To find the bind address, after starting the gateway with Luna disabled, run `docker exec ddu-gateway python -c "import socket; print(socket.gethostbyname('host.docker.internal'))"`. Confirm the returned address is assigned to a Docker Desktop/WSL private adapter with `Get-NetIPAddress -AddressFamily IPv4`. Do not use a public or ordinary LAN interface address. Keep Windows Firewall enabled and allow the bridge port only on the Docker private adapter/profile. If the internal address is not assigned to a private Windows adapter, leave Luna disabled until the network path is configured safely.

The bridge requires bearer auth for models and completions. It never accepts a caller-supplied URL or shell command. Request-line, headers, and body reads share a 10-second total deadline; completion processing starts after that read deadline is canceled. Its loopback default remains available for local-only use; the Docker deployment override is a private-interface bind because the gateway reaches the host through `host.docker.internal`.

Stop the foreground bridge with `Ctrl+C`. Confirm that the loopback listener is gone before starting it again:

```powershell
Get-NetTCPConnection -LocalAddress $env:LOCALAI_CODEX_BRIDGE_HOST -LocalPort 18765 -State Listen -ErrorAction SilentlyContinue
```

If another process owns the port, identify it before taking action; do not stop an unrelated process to free the port.

## Authentication and isolation limits

This bridge uses the existing Codex CLI sign-in for the Windows user. That account session is not an OpenAI API key and does not make this bridge equivalent to arbitrary API credentials. The CLI runs as that Windows user. Its ephemeral directory, disabled tools, and read-only mode reduce what a completion can change, but they are not a separate operating-system security boundary. Keep the listener on loopback for local-only use or on the single private Docker adapter address for gateway use, use an independent bridge token, and send only prompts approved for the cloud provider.
