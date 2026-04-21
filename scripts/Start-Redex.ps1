param(
    [switch]$RestartCodex,
    [switch]$OpenBrowser,
    [switch]$TailnetServe,
    [string]$ListenHost = "127.0.0.1",
    [int]$Port = 8765,
    [string]$CodexCliPath = "C:\Users\peter\repo\codex\codex-rs\target-redex\debug\codex.exe"
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$redexEntry = Join-Path $repoRoot "redex.py"
$stdoutLog = Join-Path $repoRoot "redex-serve.out.log"
$stderrLog = Join-Path $repoRoot "redex-serve.err.log"

if (-not (Test-Path -LiteralPath $redexEntry)) {
    throw "Could not find redex.py at $redexEntry"
}

& "$PSScriptRoot\Start-CodexRepoCore.ps1" -Restart:$RestartCodex -CodexCliPath $CodexCliPath

$existing = Get-CimInstance Win32_Process |
    Where-Object {
        $_.Name -eq "python.exe" -and
        $_.CommandLine -like "*redex.py serve*" -and
        $_.CommandLine -like "*--port $Port*"
    }

if ($existing) {
    $existing | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
}

$process = Start-Process `
    -FilePath "python" `
    -ArgumentList @("redex.py", "serve", "--host", $ListenHost, "--port", "$Port") `
    -WorkingDirectory $repoRoot `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog `
    -WindowStyle Hidden `
    -PassThru

Start-Sleep -Seconds 2

try {
    $health = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$Port/healthz"
    Write-Host "Redex is up on http://$ListenHost`:$Port"
    Write-Host "  PID=$($process.Id)"
    Write-Host "  Health=$($health.Content.Trim())"
} catch {
    Write-Warning "Redex started but health check failed. See logs:"
    Write-Warning "  $stdoutLog"
    Write-Warning "  $stderrLog"
    throw
}

if ($TailnetServe) {
    & tailscale serve --bg --https 443 "http://127.0.0.1:$Port"
    Write-Host "Published through Tailscale Serve on tailnet HTTPS port 443 -> http://127.0.0.1:$Port"
}

if ($OpenBrowser) {
    Start-Process "http://127.0.0.1:$Port/"
}
