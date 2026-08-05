$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $root 'backend'
$frontendDir = Join-Path $root 'frontend'
$processes = @()

function Stop-ProcessTree([System.Diagnostics.Process] $Process) {
    if ($null -eq $Process -or $Process.HasExited) { return }

    Get-CimInstance Win32_Process -Filter "ParentProcessId = $($Process.Id)" -ErrorAction SilentlyContinue |
        ForEach-Object { Stop-ProcessTree (Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue) }

    Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
}

try {
    $python = Join-Path $backendDir '.venv\Scripts\python.exe'
    if (-not (Test-Path $python)) {
        throw "Backend virtual environment not found at $python. Create it using the setup instructions in README.md."
    }
    if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) {
        throw 'npm is required to run the frontend.'
    }

    $processes += Start-Process -FilePath $python -ArgumentList '-m','uvicorn','app.main:app','--reload','--host','127.0.0.1','--port','8000' -WorkingDirectory $backendDir -PassThru
    $processes += Start-Process -FilePath 'npm.cmd' -ArgumentList 'run','dev','--','--host','127.0.0.1' -WorkingDirectory $frontendDir -PassThru

    Write-Host ''
    Write-Host 'NYC Sanitation Map is running at:' -ForegroundColor Green
    Write-Host '  http://127.0.0.1:5173' -ForegroundColor Cyan
    Write-Host ''
    Write-Host 'Leave this window open. Press Ctrl+C or close it to stop the app.'
    Write-Host ''

    while ($processes | Where-Object { -not $_.HasExited }) {
        Start-Sleep -Seconds 1
    }
}
catch {
    Write-Host "`nUnable to start the app: $($_.Exception.Message)" -ForegroundColor Red
    Read-Host 'Press Enter to close'
    exit 1
}
finally {
    foreach ($process in $processes) { Stop-ProcessTree $process }
}
