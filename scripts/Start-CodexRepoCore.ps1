param(
    [switch]$Restart,
    [string]$CodexCliPath = "C:\Users\peter\repo\codex\codex-rs\target-redex\debug\codex.exe"
)

$ErrorActionPreference = "Stop"

& "$PSScriptRoot\Start-CodexWithRepoCore.ps1" -Restart:$Restart -CodexCliPath $CodexCliPath
