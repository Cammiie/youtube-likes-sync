[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$EngineDirectory,
      [Parameter(Mandatory=$true)][string]$InnoCompiler)
$ErrorActionPreference = 'Stop'
Push-Location (Split-Path $PSScriptRoot -Parent)
try {
    & '.\.venv\Scripts\python.exe' tools/prepare_bundle.py --engine $EngineDirectory
    if ($LASTEXITCODE) { throw 'Could not prepare bundle assets.' }
    & '.\.venv\Scripts\python.exe' -m PyInstaller --noconfirm --clean packaging/app.spec
    if ($LASTEXITCODE) { throw 'Could not build the Windows application.' }
    & '.\dist\YouTubeLikesSync\YouTubeLikesSync.Console.exe' self-test
    if ($LASTEXITCODE) { throw 'Bundled application checks failed.' }
    & $InnoCompiler packaging/installer.iss
    if ($LASTEXITCODE) { throw 'Could not build the installer.' }
} finally { Pop-Location }
