param(
    [ValidateSet("Ensure", "Check")]
    [string]$Mode = "Ensure",
    [switch]$NonInteractive,
    [string]$Root = $PSScriptRoot
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path -LiteralPath $Root).Path
$ManifestPath = Join-Path $Root "runtime-versions.json"
$LockPath = Join-Path $Root "requirements.lock.txt"
$TemplatePath = Join-Path $Root "local_settings.example.json"
$SettingsPath = Join-Path $Root "local_settings.json"
$RuntimeRoot = Join-Path $Root ".runtime"
$CacheRoot = Join-Path $Root ".bootstrap-cache"
$VenvRoot = Join-Path $Root ".venv"
$StatePath = Join-Path $RuntimeRoot "bootstrap-state.json"
$WebRoot = Join-Path $Root "web"

function Write-Step([string]$Message) {
    Write-Host "[bootstrap] $Message"
}

function Get-Sha256([string]$Path) {
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}

function Read-Json([string]$Path) {
    return Get-Content -Raw -LiteralPath $Path | ConvertFrom-Json
}

function Invoke-Python([string]$Python, [string[]]$Arguments) {
    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed ($LASTEXITCODE): $Python $($Arguments -join ' ')"
    }
}

function Get-PythonVersion([string]$Python) {
    $value = & $Python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"
    if ($LASTEXITCODE -ne 0) { return "" }
    return ([string]$value).Trim()
}

function Get-PythonCommand([string]$ExpectedVersion) {
    $localPython = Join-Path $RuntimeRoot "python-$ExpectedVersion\python.exe"
    if (Test-Path -LiteralPath $localPython) {
        if ((Get-PythonVersion $localPython) -eq $ExpectedVersion) { return $localPython }
    }

    foreach ($candidate in @("py", "python")) {
        try {
            if ($candidate -eq "py") {
                $version = & py -3.12 -c "import sys; print(sys.executable)" 2>$null
                if ($LASTEXITCODE -eq 0 -and $version) {
                    $candidatePath = ([string]$version).Trim()
                    if ((Get-PythonVersion $candidatePath) -eq $ExpectedVersion) { return $candidatePath }
                }
            } else {
                $command = Get-Command $candidate -ErrorAction SilentlyContinue
                if ($command -and (Get-PythonVersion $command.Source) -eq $ExpectedVersion) { return $command.Source }
            }
        } catch {
            continue
        }
    }
    return $null
}

function Ensure-PythonRuntime($Manifest) {
    $version = [string]$Manifest.python.version
    $python = Get-PythonCommand $version
    if ($python) { return $python }

    $installerUrl = [string]$Manifest.python.installer_url
    $expectedHash = ([string]$Manifest.python.sha256).ToLowerInvariant()
    if ($version -ne "3.12.10" -or -not $expectedHash -or $expectedHash.Length -ne 64) {
        throw "runtime-versions.json 的 Python 版本或 SHA-256 校验值无效。"
    }
    New-Item -ItemType Directory -Force -Path $CacheRoot, $RuntimeRoot | Out-Null
    $installer = Join-Path $CacheRoot "python-$version-amd64.exe"
    if (-not (Test-Path -LiteralPath $installer) -or (Get-Sha256 $installer) -ne $expectedHash) {
        $partial = "$installer.part"
        Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
        Write-Step "正在下载 Python $version..."
        Invoke-WebRequest -UseBasicParsing -Uri $installerUrl -OutFile $partial
        if ((Get-Sha256 $partial) -ne $expectedHash) {
            Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
            throw "Python 安装器 SHA-256 校验失败。"
        }
        Move-Item -LiteralPath $partial -Destination $installer -Force
    }

    $target = Join-Path $RuntimeRoot "python-$version"
    if (-not (Test-Path -LiteralPath (Join-Path $target "python.exe"))) {
        New-Item -ItemType Directory -Force -Path $target | Out-Null
        $arguments = @(
            "/quiet", "InstallAllUsers=0", "Include_launcher=0", "Include_pip=1",
            "Include_test=0", "PrependPath=0", "Shortcuts=0", "TargetDir=$target"
        )
        $process = Start-Process -FilePath $installer -ArgumentList $arguments -Wait -PassThru
        if ($process.ExitCode -ne 0) { throw "Python 安装失败，退出码 $($process.ExitCode)。" }
    }
    $python = Join-Path $target "python.exe"
    if ((Get-PythonVersion $python) -ne $version) { throw "准备好的 Python 版本不是 $version。" }
    return $python
}

function Ensure-Venv([string]$Python, [string]$ExpectedVersion) {
    $venvPython = Join-Path $VenvRoot "Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython) {
        if ((Get-PythonVersion $venvPython) -eq $ExpectedVersion) { return $venvPython }
    }
    New-Item -ItemType Directory -Force -Path $VenvRoot | Out-Null
    Invoke-Python $Python @("-m", "venv", $VenvRoot, "--clear")
    if (-not (Test-Path -LiteralPath $venvPython)) { throw "虚拟环境创建失败。" }
    return $venvPython
}

function Ensure-Dependencies([string]$VenvPython, [string]$LockHash, $State) {
    if ($State.requirements_sha256 -eq $LockHash -and (Test-Path -LiteralPath (Join-Path $VenvRoot "Scripts\pip.exe"))) {
        return
    }
    Write-Step "正在安装 Python 依赖..."
    Invoke-Python $VenvPython @("-m", "pip", "install", "--disable-pip-version-check", "--requirement", $LockPath)
}

function Ensure-Chromium([string]$VenvPython, [string]$LockHash, $State) {
    New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null
    $marker = Join-Path $RuntimeRoot "playwright-chromium.ready"
    if ($State.playwright_sha256 -eq $LockHash -and (Test-Path -LiteralPath $marker)) { return }
    Write-Step "正在安装 Playwright Chromium..."
    Invoke-Python $VenvPython @("-m", "playwright", "install", "chromium")
    Set-Content -LiteralPath $marker -Value (Get-Date -Format o) -Encoding UTF8
}

function Ensure-LocalSettings {
    if (Test-Path -LiteralPath $SettingsPath) { return }
    if (-not (Test-Path -LiteralPath $TemplatePath)) { throw "缺少 local_settings.example.json。" }
    Copy-Item -LiteralPath $TemplatePath -Destination $SettingsPath
    Write-Step "已从配置模板创建 local_settings.json，请在网页中填写 API Key。"
}

function Ensure-WebAssets {
    $index = Join-Path $WebRoot "index.html"
    if (-not (Test-Path -LiteralPath $index)) { throw "缺少 web/index.html，请先构建前端。" }
    $assets = Join-Path $WebRoot "assets"
    if (-not (Test-Path -LiteralPath $assets) -or -not (Get-ChildItem -LiteralPath $assets -File | Select-Object -First 1)) {
        throw "缺少 web/assets，请先构建前端。"
    }
}

function Write-State($Manifest, [string]$ManifestHash, [string]$LockHash) {
    $state = [ordered]@{
        bootstrap_version = [int]$Manifest.bootstrap_version
        python_version = [string]$Manifest.python.version
        runtime_manifest_sha256 = $ManifestHash
        requirements_sha256 = $LockHash
        playwright_sha256 = $LockHash
    }
    New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null
    $temporary = "$StatePath.$PID.tmp"
    $state | ConvertTo-Json | Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Item -LiteralPath $temporary -Destination $StatePath -Force
}

function Check-Environment($Manifest, [string]$ManifestHash, [string]$LockHash) {
    Ensure-WebAssets
    if (-not (Test-Path -LiteralPath $SettingsPath)) { throw "缺少 local_settings.json。请先运行 Ensure。" }
    $venvPython = Join-Path $VenvRoot "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $venvPython)) { throw "缺少 .venv，请先运行 Ensure。" }
    if ((Get-PythonVersion $venvPython) -ne ([string]$Manifest.python.version)) { throw ".venv 不是清单指定的 Python 版本。" }
    $state = if (Test-Path -LiteralPath $StatePath) { Read-Json $StatePath } else { $null }
    if (-not $state -or $state.runtime_manifest_sha256 -ne $ManifestHash -or $state.requirements_sha256 -ne $LockHash) {
        throw "依赖状态已过期，请重新运行 Ensure。"
    }
    Write-Step "环境检查通过。"
}

try {
    if (-not (Test-Path -LiteralPath $ManifestPath)) { throw "缺少 runtime-versions.json。" }
    if (-not (Test-Path -LiteralPath $LockPath)) { throw "缺少 requirements.lock.txt。" }
    $manifest = Read-Json $ManifestPath
    $manifestHash = Get-Sha256 $ManifestPath
    $lockHash = Get-Sha256 $LockPath

    if ($Mode -eq "Check") {
        Check-Environment $manifest $manifestHash $lockHash
        exit 0
    }

    $python = Ensure-PythonRuntime $manifest
    $venvPython = Ensure-Venv $python ([string]$manifest.python.version)
    $state = if (Test-Path -LiteralPath $StatePath) { Read-Json $StatePath } else { [pscustomobject]@{} }
    Ensure-Dependencies $venvPython $lockHash $state
    Ensure-Chromium $venvPython $lockHash $state
    Ensure-LocalSettings
    Ensure-WebAssets
    Write-State $manifest $manifestHash $lockHash
    Write-Step "环境准备完成。"
    exit 0
} catch {
    Write-Error $_.Exception.Message
    if (-not $NonInteractive) { Read-Host "按 Enter 退出" | Out-Null }
    exit 1
}
