# 本地开发助手（PowerShell / PyCharm 终端）
#
#   .\scripts\bi.ps1 run  -Env dev -Port 8100    启动后端服务（热重载）
#   .\scripts\bi.ps1 test                        运行后端 pytest
#   .\scripts\bi.ps1 seed -Rows 100000           重新生成两套合成数据
#
param(
    [Parameter(Position = 0)]
    [ValidateSet('run', 'test', 'seed')]
    [string]$Command = 'run',

    [string]$Env = 'dev',
    [int]$Port = 8100,
    [int]$Rows = 100000
)

$ErrorActionPreference = 'Stop'

# 修正 Windows PowerShell 5.1 控制台/脚本编码，避免中文输出乱码（如"启动后端"显示成"鍚姩鍚庣"）
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    [Console]::InputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {
    # PowerShell 7+ 或非控制台宿主时忽略
}

$Py = 'C:\Users\William Y Liang\.workbuddy\binaries\python\envs\default\Scripts\python.exe'
$Root = Split-Path -Parent $PSScriptRoot

if (-not (Test-Path $Py)) {
    Write-Error "未找到 managed venv 解释器：$Py`n请先执行：& 'C:\Users\William Y Liang\.workbuddy\binaries\python\versions\3.13.12\python.exe' -m venv 'C:\Users\William Y Liang\.workbuddy\binaries\python\envs\default'"
}

switch ($Command) {
    'run' {
        $env:APP_ENV = $Env
        $env:PYTHONPATH = Join-Path $Root 'backend'
        Write-Host "启动后端  env=$Env  http://127.0.0.1:$Port/docs"
        Push-Location (Join-Path $Root 'backend')
        try { & $Py -m uvicorn app.main:app --reload --host 127.0.0.1 --port $Port }
        finally { Pop-Location }
    }
    'test' {
        $env:APP_ENV = 'test'
        $env:PYTHONPATH = Join-Path $Root 'backend'
        Push-Location (Join-Path $Root 'backend')
        try { & $Py -m pytest -v }
        finally { Pop-Location }
    }
    'seed' {
        & $Py (Join-Path $Root 'scripts/gen_synthetic_data.py') --dataset both --rows $Rows
    }
}
