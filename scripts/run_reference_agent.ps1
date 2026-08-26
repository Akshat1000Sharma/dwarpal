# Run the published AP2 reference shopping agent against a running Dwarpal.
#
#   .\scripts\run_reference_agent.ps1
#   .\scripts\run_reference_agent.ps1 -Base https://your-tunnel.ngrok-free.dev
#   .\scripts\run_reference_agent.ps1 -PrepareOnly
#
# Start Dwarpal first: cd backend; uvicorn main:app --port 8000
# The upstream samples are fetched into .reference-agent/, which is gitignored.

[CmdletBinding()]
param(
    [string]$Base = "http://127.0.0.1:8000",
    [switch]$PrepareOnly,
    [switch]$InstallUv
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $repoRoot "backend"

$python = Join-Path $backend ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

$arguments = @("interop/reference_agent/run_upstream_agent.py", "--base", $Base)
if ($PrepareOnly) { $arguments += "--prepare-only" }
if ($InstallUv) { $arguments += "--install-uv" }

Push-Location $backend
try {
    & $python @arguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
