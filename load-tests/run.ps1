<#
.SYNOPSIS
    Trinity load test runner.

.DESCRIPTION
    Installs locust if needed, runs the selected load shape headlessly,
    and opens the HTML report when done.

.PARAMETER Shape
    Load shape to run: ramp (default), steady, spike, stress.

.PARAMETER Host
    Base URL of the Trinity backend. Default: http://localhost:8000

.PARAMETER AdminEmail
    Admin account email (enables AdminUser persona).

.PARAMETER AdminPassword
    Admin account password.

.PARAMETER OpenReport
    Open the HTML report in the browser when the test finishes (default: $true).

.EXAMPLE
    .\run.ps1
    .\run.ps1 -Shape spike
    .\run.ps1 -Shape stress -AdminEmail admin@example.com -AdminPassword s3cr3t
    .\run.ps1 -Shape steady -Host http://192.168.1.100:8000
#>

param(
    [ValidateSet("ramp", "steady", "spike", "stress")]
    [string]$Shape = "ramp",

    [string]$Host = "http://localhost:8000",

    [string]$AdminEmail = $env:LT_ADMIN_EMAIL,
    [string]$AdminPassword = $env:LT_ADMIN_PASSWORD,

    [bool]$OpenReport = $true
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir = $PSScriptRoot
$ReportHtml = Join-Path $ScriptDir "report_${Shape}.html"
$ReportCsv  = Join-Path $ScriptDir "report_${Shape}"

# ── 1. Ensure locust is available ──────────────────────────────────────────────
if (-not (Get-Command locust -ErrorAction SilentlyContinue)) {
    Write-Host "locust not found – installing..." -ForegroundColor Yellow
    pip install -r (Join-Path $ScriptDir "requirements.txt")
}

# ── 2. Build environment for the child process ─────────────────────────────────
$env:LOAD_SHAPE = $Shape
if ($AdminEmail)    { $env:LT_ADMIN_EMAIL    = $AdminEmail }
if ($AdminPassword) { $env:LT_ADMIN_PASSWORD = $AdminPassword }

# ── 3. Run ─────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Trinity Load Test  |  shape: $Shape"   -ForegroundColor Cyan
Write-Host "  target: $Host"                          -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

$locustArgs = @(
    "-f", (Join-Path $ScriptDir "locustfile.py"),
    "--host", $Host,
    "--headless",
    "--html", $ReportHtml,
    "--csv", $ReportCsv,
    "--loglevel", "WARNING"
)

& locust @locustArgs
$exitCode = $LASTEXITCODE

# ── 4. Summary ─────────────────────────────────────────────────────────────────
Write-Host ""
if ($exitCode -eq 0) {
    Write-Host "Test complete." -ForegroundColor Green
} else {
    Write-Host "Locust exited with code $exitCode." -ForegroundColor Yellow
}

Write-Host "HTML report : $ReportHtml"
Write-Host "CSV  stats  : ${ReportCsv}_stats.csv"
Write-Host "CSV  history: ${ReportCsv}_stats_history.csv"

if ($OpenReport -and (Test-Path $ReportHtml)) {
    Start-Process $ReportHtml
}
