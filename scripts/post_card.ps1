[CmdletBinding()]
param(
    [string[]]$Report,
    [string]$Event,
    [string]$ResultsFile,
    [string]$PredictionsDirectory
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Repository virtual-environment Python not found: $python"
}
$postCardArguments = @("-B", "-u", "-m", "pipeline.post_card")
foreach ($path in $Report) { $postCardArguments += @("--report", $path) }
if ($Event) { $postCardArguments += @("--event", $Event) }
if ($ResultsFile) { $postCardArguments += @("--results-file", $ResultsFile) }
if ($PredictionsDirectory) { $postCardArguments += @("--predictions-dir", $PredictionsDirectory) }
Push-Location $repoRoot
try {
    & $python @postCardArguments
    $resultCode = $LASTEXITCODE
}
finally { Pop-Location }
exit $resultCode
