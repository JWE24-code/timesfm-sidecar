param(
    [double]$HighThreshold = 20,
    [double]$LowThreshold = 8,
    [int]$HighStreak = 6,
    [int]$LowStreak = 12,
    [int]$PollSeconds = 10,
    [switch]$SelfTest
)

$ErrorActionPreference = 'Stop'
$Dir = if ($env:TIMESFM_HOME) { $env:TIMESFM_HOME } else { 'C:\timesfm' }
$LogFile = Join-Path $Dir 'watcher.log'
$StateFile = Join-Path $Dir 'watcher_state.json'

Add-Type -Namespace Win -Name Proc -MemberDefinition @'
[DllImport("ntdll.dll")] public static extern int NtSuspendProcess(IntPtr h);
[DllImport("ntdll.dll")] public static extern int NtResumeProcess(IntPtr h);
[DllImport("kernel32.dll", SetLastError = true)] public static extern IntPtr OpenProcess(uint access, bool inherit, int pid);
[DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr h);
'@

$script:Suspended = @()
$script:LastTransition = Get-Date -Format s

function Write-Log([string]$Message) {
    Add-Content -Path $LogFile -Value ("{0} {1}" -f (Get-Date -Format s), $Message)
}

function Write-State {
    param([bool]$Gaming, [object]$Util, [int]$High, [int]$Low, [string]$ErrorText)
    [ordered]@{
        version                 = 1
        watcher_pid             = $PID
        gaming                  = $Gaming
        suspended_pids          = @($script:Suspended)
        util_3d                 = $Util
        high_streak             = $High
        low_streak              = $Low
        error                   = $ErrorText
        last_transition         = $script:LastTransition
        last_transition_reason  = $(if ($Gaming) { 'gaming detected - timesfm suspended' } else { 'idle - timesfm free to run' })
        updated                 = (Get-Date -Format s)
    } | ConvertTo-Json -Compress | Set-Content -Path $StateFile -Encoding Ascii
}

function Get-Gpu3DUtil {
    try {
        $samples = (Get-Counter '\GPU Engine(*)\Utilization Percentage' -ErrorAction Stop).CounterSamples |
            Where-Object { $_.InstanceName -match 'engtype_3D' }
        if (-not $samples) { return $null }
        return [math]::Round((($samples | Measure-Object CookedValue -Maximum).Maximum), 2)
    } catch {
        return $null
    }
}

function Get-TimesfmPids {
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath -like '*\timesfm*' } |
        Select-Object -ExpandProperty ProcessId
}

function Open-TimesfmHandle([int]$Id) {
    [Win.Proc]::OpenProcess(0x0800, $false, $Id)
}

function Suspend-All {
    foreach ($id in (Get-TimesfmPids)) {
        if ($script:Suspended -notcontains $id) {
            $h = Open-TimesfmHandle $id
            if ($h -ne [IntPtr]::Zero) {
                try {
                    $rc = [Win.Proc]::NtSuspendProcess($h)
                    if ($rc -eq 0) {
                        $script:Suspended += $id
                        Write-Log "suspended python pid $id"
                    } else {
                        Write-Log "suspend pid $id failed rc=$rc"
                    }
                } finally {
                    [void][Win.Proc]::CloseHandle($h)
                }
            } else {
                Write-Log "open pid $id failed err=$([Runtime.InteropServices.Marshal]::GetLastWin32Error())"
            }
        }
    }
}

function Resume-All {
    foreach ($id in $script:Suspended) {
        $h = Open-TimesfmHandle $id
        if ($h -ne [IntPtr]::Zero) {
            try {
                $rc = [Win.Proc]::NtResumeProcess($h)
                Write-Log "resumed pid $id rc=$rc"
            } finally {
                [void][Win.Proc]::CloseHandle($h)
            }
        }
    }
    $script:Suspended = @()
}

if ($SelfTest) {
    $p = Start-Process ping -ArgumentList '127.0.0.1', '-n', '5' -PassThru -WindowStyle Hidden
    Start-Sleep -Milliseconds 300
    $h = [Win.Proc]::OpenProcess(0x0800, $false, $p.Id)
    $rc1 = [Win.Proc]::NtSuspendProcess($h)
    [void][Win.Proc]::CloseHandle($h)
    Start-Sleep -Seconds 7
    $aliveSuspended = -not $p.HasExited
    $h = [Win.Proc]::OpenProcess(0x0800, $false, $p.Id)
    $rc2 = [Win.Proc]::NtResumeProcess($h)
    [void][Win.Proc]::CloseHandle($h)
    $exitedAfterResume = $false
    try { Wait-Process -Id $p.Id -Timeout 8; $exitedAfterResume = $true } catch {}
    @{ suspend_rc = $rc1; alive_while_suspended = $aliveSuspended; resume_rc = $rc2; exited_after_resume = $exitedAfterResume; pass = ($rc1 -eq 0 -and $rc2 -eq 0 -and $aliveSuspended -and $exitedAfterResume) } | ConvertTo-Json -Compress
    if ($rc1 -eq 0 -and $rc2 -eq 0 -and $aliveSuspended -and $exitedAfterResume) { exit 0 } else { exit 1 }
}

$created = $false
$mutex = New-Object System.Threading.Mutex($true, 'Global\TimesFMWatcher', [ref]$created)
if (-not $created) { exit 0 }

if ((Test-Path $LogFile) -and (Get-Item $LogFile).Length -gt 1MB) {
    Move-Item -Path $LogFile -Destination "$LogFile.old" -Force
}

Write-Log "watcher started pid=$PID user=$([Environment]::UserName) high=$HighThreshold x$HighStreak low=$LowThreshold x$LowStreak poll=${PollSeconds}s"

foreach ($id in (Get-TimesfmPids)) {
    $h = Open-TimesfmHandle $id
    if ($h -ne [IntPtr]::Zero) {
        try {
            [void][Win.Proc]::NtResumeProcess($h)
            Write-Log "startup safety resume pid $id"
        } finally {
            [void][Win.Proc]::CloseHandle($h)
        }
    }
}

$gaming = $false
$high = 0
$low = 0

while ($true) {
    $util = Get-Gpu3DUtil
    if ($null -eq $util) {
        Write-State $gaming $null $high $low 'gpu counter unavailable'
        Write-Log 'gpu counter unavailable'
        Start-Sleep -Seconds 30
        continue
    }
    if (-not $gaming) {
        if ($util -ge $HighThreshold) { $high++ } else { $high = 0 }
        if ($high -ge $HighStreak) {
            $gaming = $true
            $low = 0
            $script:LastTransition = Get-Date -Format s
            Write-Log "gaming detected util=$util, suspending timesfm"
            Suspend-All
        }
    } else {
        Suspend-All
        if ($util -le $LowThreshold) { $low++ } else { $low = 0 }
        if ($low -ge $LowStreak) {
            $gaming = $false
            $high = 0
            $script:LastTransition = Get-Date -Format s
            Write-Log "gaming ended util=$util, resuming timesfm"
            Resume-All
        }
    }
    Write-State $gaming $util $high $low ''
    Start-Sleep -Seconds $PollSeconds
}
