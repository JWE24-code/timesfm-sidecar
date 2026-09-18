$ErrorActionPreference = 'Stop'
$Home_ = if ($env:TIMESFM_HOME) { $env:TIMESFM_HOME } else { 'C:\timesfm' }
$tr = "$Home_\venv\Scripts\python.exe $Home_\app\daily_job.py"
schtasks /Create /F /TN TimesFMJob /RU SYSTEM /RL HIGHEST /SC HOURLY /TR $tr | Out-Null
Write-Output 'registered'
