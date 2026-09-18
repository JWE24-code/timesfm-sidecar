$ErrorActionPreference = 'Stop'
$Home_ = if ($env:TIMESFM_HOME) { $env:TIMESFM_HOME } else { 'C:\timesfm' }
$tr = "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File $Home_\watcher.ps1"
schtasks /Create /F /TN TimesFMWatcher /RU SYSTEM /RL HIGHEST /SC ONSTART /TR $tr | Out-Null
schtasks /Create /F /TN TimesFMWatcher-Logon /RU SYSTEM /RL HIGHEST /SC ONLOGON /TR $tr | Out-Null
Write-Output 'registered'
