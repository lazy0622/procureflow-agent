param(
    [switch]$WithUpstreamUI
)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Push-Location $projectRoot
try {
    if (-not (Test-Path -LiteralPath '.env')) {
        throw 'Create .env using scripts/configure_procurement.py --model-config <local file> first.'
    }
    $composeArgs = @('compose','-f','compose.procurement.yml')
    if ($WithUpstreamUI) { $composeArgs += @('-f','compose.procurement-ui.yml') }
    & docker @composeArgs config --quiet
    if ($LASTEXITCODE -ne 0) { throw 'Compose configuration is invalid.' }
    & docker @composeArgs up -d
    if ($LASTEXITCODE -ne 0) { throw 'Container startup failed.' }
    Write-Output 'ProcureFlow workbench: http://127.0.0.1:8091/'
    Write-Output 'Skyvern API heartbeat: http://127.0.0.1:8020/api/v1/heartbeat'
    if ($WithUpstreamUI) {
        if (Get-NetTCPConnection -State Listen -LocalPort 8080 -ErrorAction SilentlyContinue) {
            throw 'Port 8080 is occupied; existing service was not stopped.'
        }
        $credentialFile = Join-Path $projectRoot '.skyvern/credentials.toml'
        if (-not (Test-Path -LiteralPath $credentialFile)) {
            throw 'Wait for Skyvern first-start bootstrap, then rerun with -WithUpstreamUI.'
        }
        $content = Get-Content -LiteralPath $credentialFile -Raw
        if ($content -notmatch 'cred="([^"]+)"') { throw 'Local Skyvern credential not ready.' }
        $apiKey = $Matches[1]
        $frontendEnv = @(
            'VITE_API_BASE_URL=http://127.0.0.1:8021/api/v1',
            'VITE_ARTIFACT_API_BASE_URL=http://127.0.0.1:8091',
            'VITE_WSS_BASE_URL=ws://127.0.0.1:8021/api/v1',
            "VITE_SKYVERN_API_KEY=$apiKey",
            'VITE_BROWSER_STREAMING_MODE=cdp'
        )
        $frontendEnv | Set-Content -LiteralPath 'skyvern-frontend/.env' -Encoding utf8
        $logDir = Join-Path $projectRoot '.local'
        New-Item -ItemType Directory -Force -Path $logDir | Out-Null
        $node = (Get-Command node -ErrorAction Stop).Source
        if (-not (Test-Path -LiteralPath 'skyvern-frontend/node_modules/vite/bin/vite.js')) {
            throw 'Run npm ci in skyvern-frontend before starting the upstream UI.'
        }
        $process = Start-Process -FilePath $node -ArgumentList @(
            'node_modules/vite/bin/vite.js','--host','127.0.0.1','--port','8080','--strictPort'
        ) -WorkingDirectory (Join-Path $projectRoot 'skyvern-frontend') -WindowStyle Hidden `
            -PassThru -RedirectStandardOutput (Join-Path $logDir 'ui.log') `
            -RedirectStandardError (Join-Path $logDir 'ui.err.log')
        $process.Id | Set-Content -LiteralPath (Join-Path $logDir 'ui.pid')
        Write-Output 'Upstream Skyvern UI: http://127.0.0.1:8080/'
    }
} finally {
    Pop-Location
}
