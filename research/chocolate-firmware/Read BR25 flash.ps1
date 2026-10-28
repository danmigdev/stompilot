# Elevates only the bounded diagnostic; does not run a firmware installer.
param([switch]$ResumeLoader)
$ErrorActionPreference = 'Stop'
$studyDirectory = $PSScriptRoot
$workspaceDirectory = Split-Path (Split-Path $studyDirectory -Parent) -Parent
$pythonExecutable = Join-Path $workspaceDirectory '.venv\Scripts\python.exe'
$diagnosticScript = Join-Path $studyDirectory 'read_br25_boot.py'
if (-not (Test-Path -LiteralPath $pythonExecutable -PathType Leaf)) { throw 'Project Python is missing.' }
if (-not (Test-Path -LiteralPath $diagnosticScript -PathType Leaf)) { throw 'Diagnostic script is missing.' }
$diagnosticArguments = '-X utf8 "' + $diagnosticScript + '" --execute'
if ($ResumeLoader) { $diagnosticArguments += ' --resume-loader' }
Start-Process -FilePath $pythonExecutable -ArgumentList $diagnosticArguments -Verb RunAs -WindowStyle Hidden
