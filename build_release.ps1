param(
    [string]$Version = "v14-20260811"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$pyinstaller = Join-Path $projectRoot ".venv\Scripts\pyinstaller.exe"
$node = "D:\nodejs\node.exe"
$releaseName = "商品图片工作流-Excel批处理版-$Version"
$releaseRoot = Join-Path $projectRoot "dist\$releaseName"
$buildRoot = Join-Path $projectRoot "build\release-$Version"
$distRoot = Join-Path $projectRoot "dist\release-$Version"
$specRoot = Join-Path $buildRoot "spec"

function Copy-Directory([string]$Source, [string]$Destination) {
    & robocopy $Source $Destination /E /R:2 /W:1 /NFL /NDL /NJH /NJS /NP
    if ($LASTEXITCODE -gt 7) {
        throw "Failed to copy release dependency: $Source"
    }
}

foreach ($required in @($python, $pyinstaller, $node, (Join-Path $projectRoot "web"), (Join-Path $projectRoot "spreadsheet_runtime\exporter.mjs"))) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Missing required build dependency: $required"
    }
}

Remove-Item -LiteralPath $buildRoot -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $distRoot -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $releaseRoot -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $buildRoot, $distRoot, $specRoot, $releaseRoot | Out-Null

& $pyinstaller --noconfirm --clean --windowed --onedir --name "ProductImageWorkflow" --distpath $distRoot --workpath $buildRoot --specpath $specRoot --add-data "$projectRoot\web;web" "$projectRoot\web_app.py"
if ($LASTEXITCODE -ne 0) { throw "Main program build failed." }

& $pyinstaller --noconfirm --clean --console --onefile --name "store_insight_collector" --distpath $distRoot --workpath $buildRoot --specpath $specRoot --collect-all playwright "$projectRoot\store_insight_collector.py"
if ($LASTEXITCODE -ne 0) { throw "Store Insight collector build failed." }

& $pyinstaller --noconfirm --clean --console --onefile --name "same_item_collector" --distpath $distRoot --workpath $buildRoot --specpath $specRoot --collect-all playwright --collect-all openpyxl --collect-all PIL --hidden-import parameter_collector "$projectRoot\same_item_collector.py"
if ($LASTEXITCODE -ne 0) { throw "Same-item collector build failed." }

Copy-Item -Path (Join-Path $distRoot "ProductImageWorkflow\*") -Destination $releaseRoot -Recurse -Force
Copy-Item -LiteralPath (Join-Path $distRoot "store_insight_collector.exe") -Destination $releaseRoot -Force
Copy-Item -LiteralPath (Join-Path $distRoot "same_item_collector.exe") -Destination $releaseRoot -Force
foreach ($file in @("启动程序.bat", "README.md", "操作说明书.md", "local_settings.example.json")) {
    Copy-Item -LiteralPath (Join-Path $projectRoot $file) -Destination $releaseRoot -Force
}
$sourceSettingsPath = Join-Path $projectRoot "local_settings.json"
$templateSettingsPath = Join-Path $projectRoot "local_settings.example.json"
$settingsSourcePath = if (Test-Path -LiteralPath $sourceSettingsPath) { $sourceSettingsPath } else { $templateSettingsPath }
$sourceSettings = Get-Content -Raw -LiteralPath $settingsSourcePath | ConvertFrom-Json
$baseUrl = [string]$sourceSettings.base_url
if (-not $baseUrl.StartsWith("http")) {
    throw "Release configuration requires a valid base_url."
}
$releaseSettings = [ordered]@{
    base_url = $baseUrl
    browser_choice = ""
}
if ($null -ne $sourceSettings.oss) {
    $releaseSettings.oss = [ordered]@{
        endpoint = [string]$sourceSettings.oss.endpoint
        bucket = [string]$sourceSettings.oss.bucket
        prefix = [string]$sourceSettings.oss.prefix
    }
}
$releaseSettingsJson = $releaseSettings | ConvertTo-Json -Depth 4
$utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText((Join-Path $releaseRoot "local_settings.json"), $releaseSettingsJson + "`n", $utf8WithoutBom)
New-Item -ItemType Directory -Path (Join-Path $releaseRoot "docs") -Force | Out-Null
foreach ($document in @("oss-shared-library-setup.md", "douyin-direct-replace-user-guide.md")) {
    Copy-Item -LiteralPath (Join-Path $projectRoot "docs\$document") -Destination (Join-Path $releaseRoot "docs\$document") -Force
}
New-Item -ItemType Directory -Path (Join-Path $releaseRoot "runtime"), (Join-Path $releaseRoot "spreadsheet_runtime"), (Join-Path $releaseRoot "outputs") | Out-Null
Copy-Item -LiteralPath $node -Destination (Join-Path $releaseRoot "runtime\node.exe") -Force
Copy-Item -LiteralPath (Join-Path $projectRoot "spreadsheet_runtime\exporter.mjs") -Destination (Join-Path $releaseRoot "spreadsheet_runtime\exporter.mjs") -Force
Copy-Directory (Join-Path $projectRoot "spreadsheet_runtime\node_modules") (Join-Path $releaseRoot "spreadsheet_runtime\node_modules")

$archive = Join-Path $projectRoot "dist\$releaseName.zip"
Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
Compress-Archive -LiteralPath $releaseRoot -DestinationPath $archive -CompressionLevel Optimal

Write-Host "RELEASE_ROOT=$releaseRoot"
Write-Host "ARCHIVE=$archive"
