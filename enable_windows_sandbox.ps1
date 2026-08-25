# Enable Windows Sandbox for Opus cyber box. Must run elevated.
$ErrorActionPreference = "Continue"
$log = Join-Path $PSScriptRoot "sandbox-enable.log"
function Log([string]$msg) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -LiteralPath $log -Value $line
    Write-Host $line
}
Set-Content -LiteralPath $log -Value "Opus Windows Sandbox enable"
try {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $admin = (New-Object Security.Principal.WindowsPrincipal $id).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
    Log ("admin=" + $admin)
    if (-not $admin) {
        Log "not elevated"
        exit 2
    }
    $before = Get-WindowsOptionalFeature -Online -FeatureName Containers-DisposableClientVM
    Log ("before=" + $before.State)
    if ($before.State -eq "Enabled") {
        Log "already enabled"
        Log "reboot_required=0"
        exit 0
    }
    Log "enabling Containers-DisposableClientVM"
    $result = Enable-WindowsOptionalFeature -Online -FeatureName Containers-DisposableClientVM -All -NoRestart
    Log ("restart_needed=" + [bool]$result.RestartNeeded)
    $after = Get-WindowsOptionalFeature -Online -FeatureName Containers-DisposableClientVM
    Log ("after=" + $after.State)
    if ($result.RestartNeeded -or $after.State -ne "Enabled") {
        Log "reboot_required=1"
    } else {
        Log "reboot_required=0"
    }
    Log "ok"
    exit 0
} catch {
    Log ("error=" + $_.Exception.Message)
    exit 1
}
