<#
.SYNOPSIS
    Expose the local backend so Razorpay and Meta can reach their webhooks.

.DESCRIPTION
    Starts ngrok against the backend port, reads the public URL back from ngrok's local API, and
    prints the two callback URLs to paste into the two dashboards plus the PUBLIC_BASE_URL line
    for backend/.env.

    ngrok is invoked by full path because an MSIX install places it in the WindowsApps alias
    directory, which is not on the PATH of a non-interactive shell.

.EXAMPLE
    ./scripts/tunnel.ps1
    ./scripts/tunnel.ps1 -Port 8000
#>
[CmdletBinding()]
param(
    [int] $Port = 8000,
    [int] $TimeoutSeconds = 30
)

$ErrorActionPreference = "Stop"

function Resolve-Ngrok {
    $command = Get-Command ngrok -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }

    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps\ngrok.exe"),
        (Join-Path $env:ProgramFiles "ngrok\ngrok.exe"),
        (Join-Path $env:LOCALAPPDATA "ngrok\ngrok.exe")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) { return $candidate }
    }
    throw "ngrok was not found. Install it from https://ngrok.com/download and run 'ngrok config add-authtoken <token>'."
}

$ngrok = Resolve-Ngrok
Write-Host "Using $ngrok"

Get-Process ngrok -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1

$process = Start-Process -PassThru -WindowStyle Hidden $ngrok -ArgumentList "http", "$Port"
Write-Host "ngrok started as process $($process.Id), forwarding to port $Port"

$publicUrl = $null
for ($i = 0; $i -lt $TimeoutSeconds; $i++) {
    Start-Sleep -Seconds 1
    try {
        $tunnels = Invoke-RestMethod "http://127.0.0.1:4040/api/tunnels" -TimeoutSec 5
        if ($tunnels.tunnels.Count -gt 0) {
            $publicUrl = $tunnels.tunnels[0].public_url
            break
        }
    } catch {
        continue
    }
}

if (-not $publicUrl) {
    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    throw "ngrok did not report a public URL within $TimeoutSeconds seconds."
}

Write-Host ""
Write-Host "Public origin : $publicUrl"
Write-Host "Inspector     : http://127.0.0.1:4040"
Write-Host ""
Write-Host "Put this in backend/.env:"
Write-Host "  PUBLIC_BASE_URL=$publicUrl"
Write-Host ""
Write-Host "Razorpay webhook URL (Account and Settings, Webhooks, in Test Mode):"
Write-Host "  $publicUrl/webhooks/razorpay"
Write-Host ""
Write-Host "Meta WhatsApp callback URL (WhatsApp, Configuration, Webhooks):"
Write-Host "  $publicUrl/webhooks/whatsapp"
Write-Host ""
Write-Host "The backend must be running and PUBLIC_BASE_URL must match before you press Verify"
Write-Host "and Save in the Meta dashboard, because Meta immediately calls the callback URL."
Write-Host ""
Write-Host "Press Ctrl+C to stop the tunnel."

try {
    Wait-Process -Id $process.Id
} finally {
    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    Write-Host "Tunnel stopped."
}
