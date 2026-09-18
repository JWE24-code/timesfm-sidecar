$ErrorActionPreference = 'Stop'
$Home_ = if ($env:TIMESFM_HOME) { $env:TIMESFM_HOME } else { 'C:\timesfm' }
$tr = "$Home_\venv\Scripts\python.exe $Home_\app\serve.py"
schtasks /Create /F /TN TimesFMAPI /RU SYSTEM /RL HIGHEST /SC ONSTART /TR $tr | Out-Null
schtasks /Create /F /TN TimesFMAPI-Logon /RU SYSTEM /RL HIGHEST /SC ONLOGON /TR $tr | Out-Null
Write-Output 'registered'
