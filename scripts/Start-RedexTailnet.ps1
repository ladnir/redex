param(
    [switch]$RestartCodex,
    [switch]$OpenBrowser,
    [int]$Port = 8765,
    [string]$CodexCliPath = "C:\Users\peter\repo\codex\codex-rs\target-redex\debug\codex.exe"
)

$ErrorActionPreference = "Stop"

& "$PSScriptRoot\Start-Redex.ps1" `
    -RestartCodex:$RestartCodex `
    -OpenBrowser:$OpenBrowser `
    -TailnetServe `
    -Port $Port `
    -CodexCliPath $CodexCliPath
