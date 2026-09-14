[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$TaskName = "UFCAgent Weekly Data Refresh",
    [ValidateSet("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")]
    [string]$DayOfWeek = "Monday",
    [datetime]$At = "06:00",
    [ValidateRange(0, 10)]
    [int]$RetryCount = 3,
    [ValidateRange(1, 1440)]
    [int]$RetryIntervalMinutes = 30,
    [System.Management.Automation.PSCredential]$Credential
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $PSScriptRoot "update_ufc_data.ps1"
if (-not (Test-Path -LiteralPath $runner)) {
    throw "Refresh script not found: $runner"
}

$powershell = (Get-Command powershell.exe -ErrorAction Stop).Source
$escapedRunner = $runner.Replace('"', '""')
$actionArguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$escapedRunner`""
$action = New-ScheduledTaskAction `
    -Execute $powershell `
    -Argument $actionArguments `
    -WorkingDirectory $repoRoot
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $DayOfWeek -At $At
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 8) `
    -MultipleInstances IgnoreNew `
    -RestartCount $RetryCount `
    -RestartInterval (New-TimeSpan -Minutes $RetryIntervalMinutes)

if ($PSCmdlet.ShouldProcess($TaskName, "Register weekly UFC data refresh")) {
    $description = "Validated UFCStats refresh with bounded retries; failed candidates never replace the live database."
    if ($Credential) {
        # Register-ScheduledTask requires the plaintext value at the API
        # boundary. It is held only in this process; it is never written by
        # this repository or included in the task action.
        $taskPassword = $Credential.GetNetworkCredential().Password
        try {
            Register-ScheduledTask `
                -TaskName $TaskName `
                -Action $action `
                -Trigger $trigger `
                -Settings $settings `
                -Description $description `
                -User $Credential.UserName `
                -Password $taskPassword `
                -RunLevel Limited `
                -Force | Out-Null
        }
        finally {
            $taskPassword = $null
        }
    }
    else {
        $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        $principal = New-ScheduledTaskPrincipal `
            -UserId $currentUser `
            -LogonType Interactive `
            -RunLevel Limited
        Register-ScheduledTask `
            -TaskName $TaskName `
            -Action $action `
            -Trigger $trigger `
            -Settings $settings `
            -Description $description `
            -Principal $principal `
            -Force | Out-Null
    }

    Get-ScheduledTask -TaskName $TaskName
}
