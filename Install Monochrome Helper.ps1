$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
    & '.\.venv\Scripts\python.exe' '.\tools\install_monochrome.py'
    if ($LASTEXITCODE -ne 0) { throw 'Upstream installation failed.' }
    & '.\.venv\Scripts\python.exe' '.\tools\build_monochrome.py'
    if ($LASTEXITCODE -ne 0) { throw 'Upstream engine build failed.' }
} finally { Pop-Location }
