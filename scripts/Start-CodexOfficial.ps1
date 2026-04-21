param(
    [switch]$Restart
)

$ErrorActionPreference = "Stop"

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

Remove-Item Env:\CODEX_CLI_PATH -ErrorAction SilentlyContinue
Remove-Item Env:\CODEX_APP_SERVER_SHARE_WEBSOCKET -ErrorAction SilentlyContinue
Remove-Item Env:\CODEX_APP_SERVER_SHARE_WEBSOCKET_LISTEN -ErrorAction SilentlyContinue
Write-Host "Launching official Codex UI:"
Write-Host "  CODEX_CLI_PATH cleared for this launch"
Write-Host "  CODEX_APP_SERVER_SHARE_WEBSOCKET cleared for this launch"
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
