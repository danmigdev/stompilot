# Elevates only the validated four-sector installer/restore operation.
param([switch]$Restore)
$ErrorActionPreference = 'Stop'
$studyDirectory = $PSScriptRoot
$workspaceDirectory = Split-Path (Split-Path $studyDirectory -Parent) -Parent
$pythonExecutable = Join-Path $workspaceDirectory '.venv\Scripts\python.exe'
$installerScript = Join-Path $studyDirectory 'install_sysex_pacing.py'
if (-not (Test-Path -LiteralPath $pythonExecutable -PathType Leaf)) { throw 'Project Python is missing.' }
if (-not (Test-Path -LiteralPath $installerScript -PathType Leaf)) { throw 'Installer script is missing.' }
$installerArguments = '-X utf8 "' + $installerScript + '" --execute'
if ($Restore) { $installerArguments += ' --restore' }
Start-Process -FilePath $pythonExecutable -ArgumentList $installerArguments -Verb RunAs -WindowStyle Hidden
