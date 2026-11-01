param(
    [switch]$SkipKeyPrompt,
    [switch]$SkipDocker,
    [switch]$CheckOnly,
    [int]$Port = 0
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $scriptDir
Set-Location $root

function Write-Step([string]$msg) {
    Write-Host ("[launcher] " + $msg) -ForegroundColor Cyan
}

function Show-Message([string]$text, [string]$title = "Test Agent", [string]$kind = "Information") {
    Add-Type -AssemblyName System.Windows.Forms -ErrorAction SilentlyContinue
    $icon = switch ($kind) {
        "Error" { [System.Windows.Forms.MessageBoxIcon]::Error }
        "Warning" { [System.Windows.Forms.MessageBoxIcon]::Warning }
        "Question" { [System.Windows.Forms.MessageBoxIcon]::Question }
        default { [System.Windows.Forms.MessageBoxIcon]::Information }
    }
    [void][System.Windows.Forms.MessageBox]::Show($text, $title, [System.Windows.Forms.MessageBoxButtons]::OK, $icon)
}

function Show-Confirm([string]$text, [string]$title = "Test Agent") {
    Add-Type -AssemblyName System.Windows.Forms -ErrorAction SilentlyContinue
    $ans = [System.Windows.Forms.MessageBox]::Show($text, $title,
        [System.Windows.Forms.MessageBoxButtons]::YesNo,
        [System.Windows.Forms.MessageBoxIcon]::Question)
    return ($ans -eq [System.Windows.Forms.DialogResult]::Yes)
}

function Get-ListeningPid([int]$p) {
    try {
        $c = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($c) { return [int]$c.OwningProcess }
    } catch { }
    $line = netstat -ano | Select-String (":" + $p + " ") | Select-String "LISTENING" | Select-Object -First 1
    if ($line) {
        $parts = ($line.ToString().Trim() -split '\s+')
        return [int]$parts[-1]
    }
    return 0
}

function Get-Config {
    $path = Join-Path $scriptDir "launcher.config.json"
    $defaults = [ordered]@{
        port = 8000
        key_prompt = $true
        open_browser = $true
        docker_desktop_path = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
        docker_cli_path = "C:\Program Files\Docker\Docker\resources\bin\docker.exe"
        docker_wait_seconds = 90
        pre_start_commands = @()
    }
    if (-not (Test-Path $path)) { return [pscustomobject]$defaults }
    try {
        $raw = Get-Content $path -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($k in $defaults.Keys) {
            if ($null -eq $raw.$k) { $raw | Add-Member -NotePropertyName $k -NotePropertyValue $defaults[$k] -Force }
        }
        return $raw
    } catch {
        Write-Step ("配置读取失败，使用默认值：" + $_.Exception.Message)
        return [pscustomobject]$defaults
    }
}

function Test-PortInUse([int]$p) {
    try {
        $c = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
        if ($c) { return $true }
    } catch { }
    $line = netstat -ano | Select-String (":" + $p + " ") | Select-String "LISTENING" | Select-Object -First 1
    return [bool]$line
}

function Show-KeyDialog {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    [System.Windows.Forms.Application]::EnableVisualStyles()

    $form = New-Object System.Windows.Forms.Form
    $form.Text = "Test Agent 启动"
    $form.Size = New-Object System.Drawing.Size(460, 190)
    $form.StartPosition = "CenterScreen"
    $form.TopMost = $true
    $form.FormBorderStyle = "FixedDialog"
    $form.MaximizeBox = $false
    $form.MinimizeBox = $false

    $label = New-Object System.Windows.Forms.Label
    $label.Text = "请输入 DeepSeek API Key（仅本次启动使用，不写入磁盘）："
    $label.AutoSize = $true
    $label.Location = New-Object System.Drawing.Point(18, 18)
    $form.Controls.Add($label)

    $text = New-Object System.Windows.Forms.TextBox
    $text.Location = New-Object System.Drawing.Point(18, 48)
    $text.Size = New-Object System.Drawing.Size(420, 26)
    $text.UseSystemPasswordChar = $true
    $form.Controls.Add($text)

    $show = New-Object System.Windows.Forms.CheckBox
    $show.Text = "显示 Key"
    $show.AutoSize = $true
    $show.Location = New-Object System.Drawing.Point(18, 82)
    $show.Add_CheckedChanged({ $text.UseSystemPasswordChar = -not $show.Checked })
    $form.Controls.Add($show)

    $hint = New-Object System.Windows.Forms.Label
    $hint.Text = "留空将无法生成新用例；只看已有报告可点「仅看界面」。"
    $hint.AutoSize = $true
    $hint.ForeColor = [System.Drawing.Color]::Gray
    $hint.Location = New-Object System.Drawing.Point(105, 84)
    $form.Controls.Add($hint)

    $ok = New-Object System.Windows.Forms.Button
    $ok.Text = "启动"
    $ok.DialogResult = [System.Windows.Forms.DialogResult]::OK
    $ok.Location = New-Object System.Drawing.Point(250, 125)
    $ok.Size = New-Object System.Drawing.Size(85, 30)
    $form.Controls.Add($ok)

    $view = New-Object System.Windows.Forms.Button
    $view.Text = "仅看界面"
    $view.DialogResult = [System.Windows.Forms.DialogResult]::Ignore
    $view.Location = New-Object System.Drawing.Point(155, 125)
    $view.Size = New-Object System.Drawing.Size(85, 30)
    $form.Controls.Add($view)

    $cancel = New-Object System.Windows.Forms.Button
    $cancel.Text = "取消"
    $cancel.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
    $cancel.Location = New-Object System.Drawing.Point(345, 125)
    $cancel.Size = New-Object System.Drawing.Size(85, 30)
    $form.Controls.Add($cancel)

    $form.AcceptButton = $ok
    $form.CancelButton = $cancel
    [void]$form.ShowDialog()
    $result = $form.DialogResult
    $value = $text.Text.Trim()
    $form.Dispose()
    return [pscustomobject]@{ Result = $result; Value = $value }
}

function Get-DockerCli([object]$cfg) {
    $candidates = @()
    if ($cfg.docker_cli_path) { $candidates += $cfg.docker_cli_path }
    $cmd = Get-Command docker -ErrorAction SilentlyContinue
    if ($cmd) { $candidates += $cmd.Source }
    $candidates += "C:\Program Files\Docker\Docker\resources\bin\docker.exe"
    foreach ($c in $candidates) {
        if ($c -and (Test-Path $c)) { return $c }
    }
    return $null
}

function Test-DockerReady([string]$cli) {
    if (-not $cli) { return $false }
    try {
        & $cli info --format "{{.ServerVersion}}" *> $null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Ensure-Docker([object]$cfg) {
    $cli = Get-DockerCli $cfg
    if (Test-DockerReady $cli) {
        Write-Step "Docker 已就绪"
        return $true
    }

    $desktop = $cfg.docker_desktop_path
    if (-not (Test-Path $desktop)) {
        $candidates = @(
            "C:\Program Files\Docker\Docker\Docker Desktop.exe",
            (Join-Path $env:LOCALAPPDATA "Docker\Docker Desktop.exe")
        )
        $desktop = $null
        foreach ($c in $candidates) { if ($c -and (Test-Path $c)) { $desktop = $c; break } }
    }
    if (-not $desktop) {
        Show-Message "未找到 Docker Desktop。请把 Docker Desktop.exe 的本地路径填到 scripts\launcher.config.json 的 docker_desktop_path。" "Docker 未找到" "Warning"
        return $false
    }

    Write-Step ("启动 Docker Desktop：" + $desktop)
    Start-Process -FilePath $desktop | Out-Null
    $deadline = (Get-Date).AddSeconds([int]$cfg.docker_wait_seconds)
    $lastNotice = Get-Date
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 3
        if (Test-DockerReady $cli) {
            Write-Step "Docker daemon 已就绪"
            return $true
        }
        if (((Get-Date) - $lastNotice).TotalSeconds -ge 15) {
            Write-Step "等待 Docker daemon 启动中（首次启动可能较慢）..."
            $lastNotice = Get-Date
        }
    }
    Show-Message "Docker Desktop 已启动但 daemon 尚未就绪。可以继续打开界面，但需要容器的任务会失败。" "Docker 未就绪" "Warning"
    return $false
}

function Invoke-PreStartCommands([object]$cfg) {
    $items = @($cfg.pre_start_commands)
    foreach ($item in $items) {
        if (-not $item) { continue }
        if ($item -is [string]) {
            $cmd = $item
            $wd = $root
        } else {
            $cmd = [string]$item.command
            $wd = if ($item.working_dir) { [string]$item.working_dir } else { $root }
        }
        if (-not $cmd) { continue }
        Write-Step ("执行启动前任务：" + $cmd)
        try {
            $p = Start-Process -FilePath "powershell.exe" -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $cmd) -WorkingDirectory $wd -Wait -NoNewWindow -PassThru
            if ($p.ExitCode -ne 0) {
                Write-Step ("启动前任务退出码 " + $p.ExitCode + "（继续启动界面）")
            }
        } catch {
            Write-Step ("启动前任务失败（继续启动界面）：" + $_.Exception.Message)
        }
    }
}

function Start-BrowserWhenReady([int]$p) {
    $script = "for(`$i=0;`$i -lt 40;`$i++){try{(Invoke-WebRequest 'http://127.0.0.1:$p/' -UseBasicParsing -TimeoutSec 2)|Out-Null; Start-Process 'http://127.0.0.1:$p'; break}catch{Start-Sleep 1}}"
    Start-Process -FilePath "powershell.exe" -ArgumentList @("-NoProfile", "-WindowStyle", "Hidden", "-Command", $script) -WindowStyle Hidden | Out-Null
}

# ---------------- main ----------------
$cfg = Get-Config
$port = if ($Port -gt 0) { $Port } else { [int]$cfg.port }

if ($CheckOnly) {
    $cli = Get-DockerCli $cfg
    $python = Get-Command python -ErrorAction SilentlyContinue
    Write-Host ("project_root = " + $root)
    Write-Host ("port = " + $port + " / in_use = " + (Test-PortInUse $port))
    Write-Host ("python = " + $(if ($python) { $python.Source } else { "NOT FOUND" }))
    Write-Host ("docker_cli = " + $(if ($cli) { $cli } else { "NOT FOUND" }))
    Write-Host ("docker_ready = " + (Test-DockerReady $cli))
    Write-Host ("docker_desktop_path = " + $cfg.docker_desktop_path)
    Write-Host ("pre_start_commands = " + (@($cfg.pre_start_commands) | ConvertTo-Json -Compress))
    exit 0
}

if (Test-PortInUse $port) {
    $oldPid = Get-ListeningPid $port
    $oldProc = Get-CimInstance Win32_Process -Filter ("ProcessId = " + $oldPid) -ErrorAction SilentlyContinue
    $isOurs = $oldProc -and ($oldProc.CommandLine -match "test_agent.api.server:app")
    if (-not $isOurs) {
        Show-Message ("端口 " + $port + " 被 PID " + $oldPid + " 占用，且不是本项目服务。请改 scripts\launcher.config.json 的 port，或先手动停止该进程。") "端口被占用" "Error"
        exit 1
    }
    if (-not (Show-Confirm ("端口 " + $port + " 已有本项目的服务在运行。`n`n是否停止旧服务并重新启动（重新输入 API Key）？`n选「否」则直接打开现有界面。") "服务已在运行")) {
        Write-Step "保留旧服务，直接打开浏览器。"
        if ($cfg.open_browser) { Start-Process ("http://127.0.0.1:" + $port) }
        exit 0
    }
    Write-Step ("停止旧服务 PID " + $oldPid + " ...")
    Stop-Process -Id $oldPid -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Show-Message "未找到 python。请先安装 Python/Anaconda，并确保 python 在 PATH 中。" "Python 未找到" "Error"
    exit 1
}

if (-not $SkipKeyPrompt -and $cfg.key_prompt -ne $false) {
    $choice = $null
    while ($true) {
        $choice = Show-KeyDialog
        if ($choice.Result -eq [System.Windows.Forms.DialogResult]::Cancel) {
            Write-Step "已取消启动。"
            exit 1
        }
        if ($choice.Result -eq [System.Windows.Forms.DialogResult]::Ignore) {
            Remove-Item Env:DEEPSEEK_API_KEY -ErrorAction SilentlyContinue
            Write-Step "仅看界面：跳过 API Key（本次启动不调用 LLM）。"
            break
        }
        if ($choice.Value.Length -ge 8) {
            $env:DEEPSEEK_API_KEY = $choice.Value
            Write-Step "已接收 API Key（仅本次进程使用，不落盘）"
            break
        }
        Show-Message "Key 为空或过短。请重新输入；只看已有报告可点「仅看界面」。" "Key 无效" "Warning"
    }
}

if (-not $SkipDocker) {
    [void](Ensure-Docker $cfg)
}

Invoke-PreStartCommands $cfg

if ($cfg.open_browser) { Start-BrowserWhenReady $port }

Write-Step ("启动服务：http://127.0.0.1:" + $port)
Write-Step "关闭本窗口即停止服务（也可按 Ctrl+C）。"
$pythonPath = $python.Source
& $pythonPath -m uvicorn test_agent.api.server:app --host 127.0.0.1 --port $port
if ($LASTEXITCODE -ne 0) {
    Show-Message ("服务异常退出（exit " + $LASTEXITCODE + "）。请查看本窗口日志。") "Test Agent" "Error"
}
