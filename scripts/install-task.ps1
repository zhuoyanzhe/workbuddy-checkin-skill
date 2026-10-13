# install-task.ps1 - WorkBuddy 自动签到：注册 Windows 计划任务
#
# 参考 88lin/workbuddy-auto-signin（MIT）的 install-windows.ps1 思路重写：
#   https://github.com/88lin/workbuddy-auto-signin
#
# 用法（PowerShell，无需管理员）：
#   powershell -ExecutionPolicy Bypass -File .\install-task.ps1
#   powershell -ExecutionPolicy Bypass -File .\install-task.ps1 -Uninstall
#   powershell -ExecutionPolicy Bypass -File .\install-task.ps1 -Pythonw "D:\soft\miniconda\pythonw.exe"
#
# 注册两个任务：
#   1) WorkBuddyAutoSignin      每天 00:05                    签到（静默，写 signin.log）
#   2) WorkBuddyAutoSigninPoll  每天 01 / 05 / 09 / 13 / 17 / 21 点  补签（已签则静默）
#
# 为什么要 6 个独立触发器而不是「每天一次 + 每 4 小时重复」：后者的重复实例一旦
# 错过（关机 / 睡眠）就永久跳过，StartWhenAvailable 不会为它补跑；独立触发器则每次
# 错过都有机会在下次可用时补上。等于把签到机会从「一天一次」变成「一天七次」。
#
# 两个任务都是零 Token：直接跑本地 Python，完全不经过 AI 模型，不消耗积分。

[CmdletBinding()]
param(
    [string]$Pythonw = "",
    [string]$Script  = "",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

$TaskMain = "WorkBuddyAutoSignin"
$TaskPoll = "WorkBuddyAutoSigninPoll"

function Remove-Tasks {
    foreach ($name in @($TaskMain, $TaskPoll)) {
        $t = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if ($t) {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Host ("  已删除任务 {0}" -f $name) -ForegroundColor Green
        } else {
            Write-Host ("  任务 {0} 不存在，跳过" -f $name) -ForegroundColor DarkGray
        }
    }
}

if ($Uninstall) {
    Write-Host ""
    Write-Host "WorkBuddy 自动签到 · 卸载" -ForegroundColor Cyan
    Remove-Tasks
    Write-Host ""
    exit 0
}

function Find-Pythonw {
    # 1) PATH 里就有 pythonw
    $c = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
    if ($c -and (Test-Path $c)) { return $c }

    # 2) PATH 里有 python.exe，取同目录的 pythonw
    $py = (Get-Command python -ErrorAction SilentlyContinue).Source
    if ($py) {
        $c = Join-Path (Split-Path $py) "pythonw.exe"
        if (Test-Path $c) { return $c }
    }

    # 3) 常见安装目录（不少 Windows 装机 Python 并不进 PATH，这一步最常命中）
    foreach ($pat in @(
        "D:\soft\miniconda\pythonw.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python3*\pythonw.exe",
        "C:\Python3*\pythonw.exe",
        "C:\Program Files\Python3*\pythonw.exe"
    )) {
        $hit = Get-ChildItem $pat -ErrorAction SilentlyContinue |
               Sort-Object FullName -Descending | Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    return $null
}

Write-Host ""
Write-Host "WorkBuddy 自动签到 · 一键安装" -ForegroundColor Cyan
Write-Host ("-" * 46) -ForegroundColor DarkGray

# --- 1. 定位 pythonw.exe ---
Write-Host "[1/3] 定位 pythonw.exe ..." -NoNewline
$exe = $Pythonw
# 必须先判空再 Test-Path：Test-Path "" 抛的是参数校验异常（不是返回 $false），
# 配合顶部的 $ErrorActionPreference = "Stop" 会直接死在这一行。
if (-not $exe -or -not (Test-Path $exe)) { $exe = Find-Pythonw }
if (-not $exe -or -not (Test-Path $exe)) {
    Write-Host " 失败" -ForegroundColor Red
    Write-Host ""
    Write-Host "没找到 pythonw.exe。请先安装 Python 3：https://www.python.org/downloads/" -ForegroundColor Yellow
    Write-Host "装好后重跑；或用 -Pythonw 指定完整路径。" -ForegroundColor Yellow
    exit 1
}
$exe = (Resolve-Path $exe).Path
Write-Host " $exe" -ForegroundColor Green

# --- 2. 定位脚本 ---
Write-Host "[2/3] 定位 workbuddy_checkin.py ..." -NoNewline
$target = $Script
if (-not $target -or -not (Test-Path $target)) {
    if ($PSScriptRoot) { $target = Join-Path $PSScriptRoot "workbuddy_checkin.py" }
}
if (-not $target -or -not (Test-Path $target)) {
    $target = Join-Path (Get-Location) "workbuddy_checkin.py"
}
if (-not $target -or -not (Test-Path $target)) {
    Write-Host " 失败" -ForegroundColor Red
    Write-Host ""
    Write-Host "没找到 workbuddy_checkin.py。请把本脚本和它放在同一目录后重跑，" -ForegroundColor Yellow
    Write-Host "或用 -Script 指定完整路径。" -ForegroundColor Yellow
    exit 1
}
$target = (Resolve-Path $target).Path
Write-Host " $target" -ForegroundColor Green

# --- 3. 注册计划任务 ---
Write-Host "[3/3] 注册计划任务 ..." -NoNewline
try {
    $userId   = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive

    # 任务 1：每日签到（00:05，与签到日边界对齐）
    $act1 = New-ScheduledTaskAction -Execute $exe -Argument ('"{0}" silent' -f $target)
    $tri1 = New-ScheduledTaskTrigger -Daily -At "00:05"
    $set1 = New-ScheduledTaskSettingsSet -StartWhenAvailable -Hidden `
            -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
    Register-ScheduledTask -TaskName $TaskMain -Action $act1 -Trigger $tri1 `
        -Settings $set1 -Principal $principal -Force `
        -Description "WorkBuddy daily auto signin (silent, zero token)" | Out-Null

    # 任务 2：补签轮询（01/05/09/13/17/21 共 6 次，已签则静默不写日志）
    $tri2 = @()
    foreach ($hh in @("01:00", "05:00", "09:00", "13:00", "17:00", "21:00")) {
        $tri2 += New-ScheduledTaskTrigger -Daily -At $hh
    }
    $act2 = New-ScheduledTaskAction -Execute $exe -Argument ('"{0}" silent-poll' -f $target)
    $set2 = New-ScheduledTaskSettingsSet -StartWhenAvailable -Hidden `
            -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
    Register-ScheduledTask -TaskName $TaskPoll -Action $act2 -Trigger $tri2 `
        -Settings $set2 -Principal $principal -Force `
        -Description "WorkBuddy checkin catch-up poll (zero token)" | Out-Null
} catch {
    Write-Host " 失败" -ForegroundColor Red
    Write-Host ""
    Write-Host "错误信息：$($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
Write-Host " 完成" -ForegroundColor Green

# --- 收尾汇报 ---
Write-Host ""
Write-Host "计划任务已就位：" -ForegroundColor Green
foreach ($row in @(
    @{ Name = $TaskMain; When = "每天 00:05";        What = "签到（静默写日志）" },
    @{ Name = $TaskPoll; When = "01/05/09/13/17/21"; What = "补签（已签则静默）" }
)) {
    $t = Get-ScheduledTask -TaskName $row.Name
    $i = Get-ScheduledTaskInfo -TaskName $row.Name
    Write-Host ("  {0,-26} {1,-8} {2,-20} {3}" -f $row.Name, $t.State, $row.When, $row.What)
    Write-Host ("  {0,-26} 下次运行 {1}" -f "", $i.NextRunTime) -ForegroundColor DarkGray
}

$logFile = Join-Path (Split-Path $target) "signin.log"
Write-Host ""
Write-Host "日志：$logFile" -ForegroundColor Cyan
Write-Host "立即验证一次（不消耗积分，已签会返回 ALREADY）：" -ForegroundColor DarkGray
Write-Host ("  Start-ScheduledTask -TaskName `"{0}`"" -f $TaskMain) -ForegroundColor DarkGray
Write-Host ("  Get-Content `"$logFile`" -Tail 3") -ForegroundColor DarkGray
Write-Host ""
Write-Host "卸载：powershell -ExecutionPolicy Bypass -File .\install-task.ps1 -Uninstall" -ForegroundColor DarkGray
Write-Host ""
