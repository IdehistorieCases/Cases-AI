# Windows Codex CLI bridge

Run this bridge on the signed-in Windows host. It binds only to 127.0.0.1:18765 and is not part of Docker or the dashboard. It invokes authenticated Codex CLI in ephemeral, read-only temporary directories with high reasoning effort. Requests and access logs are not written to disk.

Set a new bridge bearer token in the current PowerShell session without echoing it:

    $secure = Read-Host "Bridge token (24+ random characters)" -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { $env:LOCALAI_CODEX_BRIDGE_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
    python integrations/codex-bridge/codex_bridge.py

The signed-in Codex CLI must already be authenticated for this Windows user. Keep this process running while Luna is enabled. The bridge rejects non-loopback bind addresses and requires bearer auth for models and completions. It never accepts a caller-supplied URL or shell command.

Stop the foreground bridge with `Ctrl+C`. Confirm that the loopback listener is gone before starting it again:

```powershell
Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 18765 -State Listen -ErrorAction SilentlyContinue
```

If another process owns the port, identify it before taking action; do not stop an unrelated process to free the port.

## Authentication and isolation limits

This bridge uses the existing Codex CLI sign-in for the Windows user. That account session is not an OpenAI API key and does not make this bridge equivalent to arbitrary API credentials. The CLI runs as that Windows user. Its ephemeral directory, disabled tools, and read-only mode reduce what a completion can change, but they are not a separate operating-system security boundary. Keep the bridge bound to loopback, use an independent bridge token, and send only prompts approved for the cloud provider.
