# Knowledge Graph RAG 一键启动
# 由桌面的 start_app.bat 调用；也可以直接右键“使用 PowerShell 运行”。
# 只跑 python app.py：FastAPI/uvicorn 同时托管打包后的前端（frontend\dist）和 /api 接口（Swagger 文档在 /docs）。
# 关闭窗口 = 停止后端（靠 Job 对象的 KILL_ON_JOB_CLOSE）。
# 调试前端：另开终端在 frontend 目录跑 npm run dev（端口 5173，会代理 /api 到 5001）。

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
try { $Host.UI.RawUI.WindowTitle = 'Knowledge Graph RAG 启动器' } catch {}
# 系统开了代理（Clash 之类）时，对 localhost 的请求会被绕一圈，慢到几秒甚至超时，
# 之前 launcher 探活用 Invoke-WebRequest 每次都超时 -> 一直以为后端没起来 -> 浏览器不打开。
try { [System.Net.WebRequest]::DefaultWebProxy = $null } catch {}

$proj     = 'E:\ailearning\LightRAG\knowledge-graph-rag'
$frontend = Join-Path $proj 'frontend'
$dist     = Join-Path $frontend 'dist'
$appUrl   = 'http://127.0.0.1:5001'   # 用 IP 不用 localhost，避开 ::1 / 代理

# 探活：直连、显式不走代理、3 秒超时
function Test-Backend {
    try {
        $req = [System.Net.HttpWebRequest]::Create("$appUrl/api/health")
        $req.Proxy = $null
        $req.Timeout = 3000
        $resp = $req.GetResponse()
        $ok = ([int]$resp.StatusCode -eq 200)
        $resp.Close()
        return $ok
    } catch { return $false }
}

function Fail($msg) {
    Write-Host ''
    Write-Host "  [错误] $msg" -ForegroundColor Red
    Write-Host ''
    Read-Host '  按回车键退出'
    exit 1
}

if (-not (Test-Path (Join-Path $proj 'app.py'))) { Fail "找不到 $proj\app.py，请确认项目路径" }
if (-not (Get-Command python -ErrorAction SilentlyContinue)) { Fail 'PATH 里找不到 python' }

# 前端没打包过就打一次（之后前端有改动，手动在 frontend 目录重跑 npm run build 即可）
if (-not (Test-Path (Join-Path $dist 'index.html'))) {
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        Fail "前端还没打包（缺 $dist\index.html），且 PATH 里没有 npm。请先装 Node 再跑 npm run build"
    }
    if (-not (Test-Path (Join-Path $frontend 'node_modules'))) {
        Write-Host '  首次运行：安装前端依赖 (npm install)...' -ForegroundColor Cyan
        Push-Location $frontend
        try { npm install } finally { Pop-Location }
    }
    Write-Host '  打包前端 (npm run build)...' -ForegroundColor Cyan
    Push-Location $frontend
    try { npm run build } finally { Pop-Location }
    if (-not (Test-Path (Join-Path $dist 'index.html'))) { Fail 'npm run build 没有生成 dist\index.html，看上面的报错' }
}

# ---------- 后端已经在跑就别重启，直接开页面（重复双击不再互相踢掉）----------
if (Test-Backend) {
    Write-Host ''
    Write-Host '  后端已经在运行，直接打开页面。' -ForegroundColor Green
    Start-Process $appUrl
    Write-Host "  已在浏览器打开  $appUrl" -ForegroundColor Green
    Write-Host ''
    Write-Host '  （本窗口不是那个服务窗口，可以直接关掉；页面没反应就关掉所有启动器窗口再双击一次）' -ForegroundColor DarkGray
    Start-Sleep -Seconds 4
    exit 0
}

# ---------- 先清掉可能残留的旧实例（否则新进程绑不上 5001 会直接退出）----------
function Stop-PortOwners([int[]]$ports) {
    $pids = Get-NetTCPConnection -LocalPort $ports -State Listen -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($processId in $pids) {
        try {
            $p = Get-Process -Id $processId -ErrorAction Stop
            Write-Host ("  端口被 {0} (pid {1}) 占用，结束旧进程" -f $p.ProcessName, $processId) -ForegroundColor DarkYellow
            Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
        } catch {}
    }
    if ($pids) { Start-Sleep -Seconds 2 }
}
Stop-PortOwners @(5001)   # 5173 是可选的前端 dev server，不去动它

# ---------- Job 对象：本窗口关闭时连带结束前后端及其所有子进程 ----------
if (-not ('Win32Job' -as [type])) {
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class Win32Job {
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    public static extern IntPtr CreateJobObject(IntPtr a, string lpName);
    [DllImport("kernel32.dll")]
    public static extern bool SetInformationJobObject(IntPtr hJob, int t, IntPtr info, uint cb);
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool AssignProcessToJobObject(IntPtr job, IntPtr proc);
}
'@
}

$Job = [Win32Job]::CreateJobObject([IntPtr]::Zero, $null)
$buf = New-Object byte[] 144
[BitConverter]::GetBytes([int]0x2000).CopyTo($buf, 16)   # LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
$mem = [Runtime.InteropServices.Marshal]::AllocHGlobal(144)
[Runtime.InteropServices.Marshal]::Copy($buf, 0, $mem, 144)
[void][Win32Job]::SetInformationJobObject($Job, 9, $mem, 144)   # 9 = JobObjectExtendedLimitInformation
[Runtime.InteropServices.Marshal]::FreeHGlobal($mem)

function Start-Server($file, $argList, $wd, $label, $logBase) {
    Write-Host "  启动 $label ..." -ForegroundColor Cyan
    $sp = @{
        FilePath         = $file
        ArgumentList     = $argList
        WorkingDirectory = $wd
        WindowStyle      = 'Hidden'
        PassThru         = $true
    }
    if ($logBase) {
        $sp.RedirectStandardOutput = (Join-Path $proj "$logBase.out.log")
        $sp.RedirectStandardError  = (Join-Path $proj "$logBase.err.log")
    }
    $p = Start-Process @sp
    try { [void][Win32Job]::AssignProcessToJobObject($Job, $p.Handle) } catch {}
    return $p
}

Write-Host ''
$backend = Start-Server 'python' @('app.py') $proj 'FastAPI/uvicorn (页面 + 接口 :5001)' $null

function Wait-Backend($maxSec) {
    Write-Host '  等待后端就绪（首次要加载向量模型，约 10-20s）...' -ForegroundColor Cyan
    for ($i = 0; $i -lt $maxSec; $i++) {
        if ($backend.HasExited) { return $false }
        if (Test-Backend) { return $true }
        Start-Sleep -Seconds 1
    }
    return $false
}

# 后端要先 import workflow + 加载向量模型（约 10s），等 /api/health 通了再开浏览器
$backendReady = Wait-Backend 120

$failed = $false
if ($backend.HasExited) {
    $failed = $true
    Write-Host "  启动失败（退出码 $($backend.ExitCode)）。看 $proj\app.log" -ForegroundColor Red
} elseif (-not $backendReady) {
    Write-Host '  120 秒内未就绪（还在加载模型？），稍后手动刷新页面即可。' -ForegroundColor Yellow
}

if ($backendReady) {
    Start-Process $appUrl
    Write-Host "  已在浏览器打开  $appUrl" -ForegroundColor Green
} elseif (-not $backend.HasExited) {
    Write-Host "  可稍后手动打开  $appUrl" -ForegroundColor Yellow
}

if ($failed) {
    Write-Host ''
    Write-Host '  （如果你刚刚重复双击了快捷方式，这个窗口可以直接关掉，另一个是好的）' -ForegroundColor DarkGray
    for ($s = 20; $s -gt 0; $s--) {
        Write-Host -NoNewline ("`r  {0} 秒后自动关闭本窗口..." -f $s)
        Start-Sleep -Seconds 1
    }
    exit 1
}

Write-Host ''
Write-Host '  ==================================================' -ForegroundColor DarkGray
Write-Host "   页面 + 接口   $appUrl" -ForegroundColor Gray
Write-Host '   关闭这个窗口  =  停止服务' -ForegroundColor Gray
Write-Host '   前端有改动：在 frontend 目录跑 npm run build，再刷新页面' -ForegroundColor Gray
Write-Host '  ==================================================' -ForegroundColor DarkGray
Write-Host ''
Write-Host '  运行中... (Ctrl+C 或直接关窗口即可停止)' -ForegroundColor DarkGray

while (-not $backend.HasExited) {
    Start-Sleep -Seconds 2
}

Write-Host ''
Write-Host '  检测到服务退出，正在清理...' -ForegroundColor Yellow
Start-Sleep -Seconds 1
# 脚本退出 -> Job 句柄关闭 -> KILL_ON_JOB_CLOSE 自动结束 python
