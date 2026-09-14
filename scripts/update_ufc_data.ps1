[CmdletBinding()]
param(
    [string]$Database,
    [string]$BootstrapFrom,
    [switch]$Full,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$AdditionalArguments
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $Database) {
    $Database = Join-Path $repoRoot "ufc_fights.db"
}

$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    throw "Repository virtual-environment Python not found: $venvPython. Run the README setup before scheduling updates."
}
$python = $venvPython

$refreshArguments = @("-B", "-m", "pipeline.weekly", "--db", $Database)
if ($BootstrapFrom) {
    $refreshArguments += @("--bootstrap-from", $BootstrapFrom)
}
if ($Full) {
    $refreshArguments += "--full"
}
if ($AdditionalArguments) {
    $refreshArguments += $AdditionalArguments
}

Push-Location $repoRoot
$refreshExitCode = 2
try {
    $logDirectory = Join-Path $repoRoot "data_runs\scheduled_logs"
    $logPath = $null
    try {
        New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
        $logName = "weekly-{0}-{1}.log" -f (Get-Date -Format "yyyyMMddTHHmmss"), $PID
        $logPath = Join-Path $logDirectory $logName
    }
    catch {
        Write-Warning "Could not create the durable refresh log: $($_.Exception.Message)"
    }
    if ($logPath) {
        # Windows PowerShell surfaces native stderr as non-terminating error
        # records. Keep them flowing through Tee-Object, then restore the
        # script's fail-fast policy for PowerShell operations.
        $savedErrorActionPreference = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            & $python @refreshArguments 2>&1 | Tee-Object -FilePath $logPath
            $refreshExitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $savedErrorActionPreference
        }
    }
    else {
        & $python @refreshArguments
        $refreshExitCode = $LASTEXITCODE
    }
}
finally {
    Pop-Location
}

exit $refreshExitCode
