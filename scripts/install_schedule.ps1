# Register (or replace) the daily JobRadar task for the current user. Remove with:
#   Unregister-ScheduledTask -TaskName JobRadar -Confirm:$false
param([string]$At = "09:00")
$script = Join-Path $PSScriptRoot "daily.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2)
Register-ScheduledTask -TaskName JobRadar -Action $action -Trigger $trigger -Settings $settings -Force
