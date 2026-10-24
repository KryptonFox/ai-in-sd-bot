$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
$bootstrap = Join-Path $PSScriptRoot 'bootstrap.py'
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    $message = [System.Text.RegularExpressions.Regex]::Unescape('\u041d\u0443\u0436\u0435\u043d uv. \u0421\u043c. README.md.')
    Write-Host $message
    exit 1
}
$project = Join-Path $PSScriptRoot '..'
& uv run --project $project --locked --no-env-file python $bootstrap @args
exit $LASTEXITCODE
