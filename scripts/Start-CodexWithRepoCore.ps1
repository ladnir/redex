param(
    [switch]$Restart,
    [string]$CodexCliPath = "C:\Users\peter\repo\codex\codex-rs\target-redex\debug\codex.exe"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $CodexCliPath)) {
    throw "Codex repo binary not found: $CodexCliPath"
}

if ($Restart) {
    Get-CimInstance Win32_Process |
        Where-Object {
            $_.Name -eq "Codex.exe" -or
            ($_.Name -eq "codex.exe" -and $_.CommandLine -like "* app-server*")
        } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
}

$package = Get-AppxPackage -Name "OpenAI.Codex" |
    Sort-Object PackageFullName -Descending |
    Select-Object -First 1

if ($null -eq $package) {
    throw "Could not find the installed Codex Windows app package."
}

$appExe = Join-Path $package.InstallLocation "app\Codex.exe"

$env:CODEX_CLI_PATH = $CodexCliPath
$env:CODEX_APP_SERVER_SHARE_WEBSOCKET = "1"
Remove-Item Env:\CODEX_APP_SERVER_SHARE_WEBSOCKET_LISTEN -ErrorAction SilentlyContinue
Write-Host "Launching Codex UI with repo core:"
Write-Host "  CODEX_CLI_PATH=$env:CODEX_CLI_PATH"
Write-Host "  CODEX_APP_SERVER_SHARE_WEBSOCKET=$env:CODEX_APP_SERVER_SHARE_WEBSOCKET"
Write-Host "  UI=$appExe"

try {
    Start-Process -FilePath $appExe
} catch {
    $appId = (Get-StartApps -Name "Codex" | Select-Object -First 1).AppID
    if (-not $appId) {
        throw
    }
    Write-Host "  Direct launch failed; falling back to Start Menu app activation."
    Start-Process -FilePath "shell:AppsFolder\$appId"
}
