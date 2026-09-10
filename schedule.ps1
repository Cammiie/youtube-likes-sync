[CmdletBinding()]
param([ValidateSet('Install','Remove')][string]$Action = 'Install')
$ErrorActionPreference = 'Stop'
$taskName = 'YouTube Likes Sync'
if ($Action -eq 'Remove') {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output 'Background task removed. Downloads and connection data are preserved.'
    exit 0
}
$projectRoot = $PSScriptRoot
$pythonWindowless = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonWindowless)) { throw 'Run install.ps1 first.' }
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$actionSpec = New-ScheduledTaskAction -Execute $pythonWindowless -Argument '-m ytlikes.cli sync --quiet' -WorkingDirectory $projectRoot
$logon = New-ScheduledTaskTrigger -AtLogOn -User $identity
$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -Hidden -StartWhenAvailable -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName $taskName -Action $actionSpec -Trigger @($logon, $repeat) -Principal $principal -Settings $settings -Description 'Fetch future YouTube Music likes and download matching Monochrome FLAC tracks. Waits safely until local account setup is complete.' -Force | Out-Null
Write-Output 'Installed: every five minutes and at sign-in; runs hidden as the current user.'
