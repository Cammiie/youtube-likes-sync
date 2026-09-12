[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
    if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
        python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 or newer is required.' }
    }
    & '.\.venv\Scripts\python.exe' -m pip install -r requirements.lock.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    & '.\.venv\Scripts\python.exe' -m pip install --no-deps --no-build-isolation -e .
    if ($LASTEXITCODE -ne 0) { throw 'Project installation failed.' }
    & '.\.venv\Scripts\python.exe' '.\tools\pin_runtime.py'
    if ($LASTEXITCODE -ne 0) { throw 'Runtime setup failed.' }
    & '.\.venv\Scripts\python.exe' '.\tools\initialize_installation.py'
    if ($LASTEXITCODE -ne 0) { throw 'Download configuration failed.' }
    & '.\.venv\Scripts\python.exe' -m ytlikes.extension_host --register
    if ($LASTEXITCODE -ne 0) { throw 'Browser connector registration failed.' }
    & '.\.venv\Scripts\python.exe' -m ytlikes.cli migrate
    if ($LASTEXITCODE -ne 0) { throw 'Quit the old downloader normally, then retry setup.' }
    $downloadEngine = & '.\.venv\Scripts\python.exe' -c 'from ytlikes.common import config,data_dir; print(config(data_dir())["download_engine"])'
    if ($LASTEXITCODE -ne 0) { throw 'Could not read download configuration.' }
    if ($downloadEngine -eq 'monochrome') {
        & (Join-Path $PSScriptRoot 'Install Monochrome Helper.ps1')
    }
    & (Join-Path $PSScriptRoot 'schedule.ps1') -Action Install
} finally { Pop-Location }
