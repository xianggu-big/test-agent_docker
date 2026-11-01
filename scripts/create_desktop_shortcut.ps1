param(
    [switch]$StartMenu,
    [string]$Name = "Test Agent"
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $scriptDir
$launcher = Join-Path $scriptDir "start_ui.ps1"
if (-not (Test-Path $launcher)) { throw "找不到启动脚本：$launcher" }

$pwsh = Get-Command pwsh -ErrorAction SilentlyContinue
$shell = if ($pwsh) { $pwsh.Source } else { "powershell.exe" }

$desktop = [Environment]::GetFolderPath("Desktop")
$targets = @(Join-Path $desktop ($Name + ".lnk"))
if ($StartMenu) {
    $sm = Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs"
    $targets += (Join-Path $sm ($Name + ".lnk"))
}

$ws = New-Object -ComObject WScript.Shell
foreach ($target in $targets) {
    $lnk = $ws.CreateShortcut($target)
    $lnk.TargetPath = $shell
    $lnk.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$launcher`""
    $lnk.WorkingDirectory = $root
    $lnk.Description = "启动 Test Agent（弹窗填 API Key，自动启动 Docker Desktop）"
    $lnk.IconLocation = "$env:SystemRoot\System32\shell32.dll,13"
    $lnk.Save()
    Write-Host ("已创建快捷方式：" + $target)
}

Write-Host ""
Write-Host "双击桌面图标即可启动。服务运行在当前控制台窗口，关闭窗口即停止服务。"
