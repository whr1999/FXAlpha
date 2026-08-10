$ErrorActionPreference = "Stop"

Write-Host "Starting FXAlpha factor-research services in WSL..."
wsl.exe -e bash -lc "systemctl --user start fxalpha-factor-stack.target"

$deadline = (Get-Date).AddSeconds(60)
$apiReady = $false
$qgptReady = $false
while ((Get-Date) -lt $deadline) {
    try {
        $api = Invoke-RestMethod -Uri "http://127.0.0.1:18081/health" -TimeoutSec 2
        $apiReady = [bool]$api.ok
    } catch { $apiReady = $false }
    try {
        $qgpt = Invoke-RestMethod -Uri "http://127.0.0.1:8003/api/v1/health" -TimeoutSec 2
        $qgptReady = $true
    } catch { $qgptReady = $false }
    if ($apiReady -and $qgptReady) { break }
    Start-Sleep -Seconds 2
}

if (-not ($apiReady -and $qgptReady)) {
    throw "FXAlpha services did not become healthy within 60 seconds. API=$apiReady QuantGPT=$qgptReady"
}

Start-Process "http://127.0.0.1:18081/gui/"
Write-Host "FXAlpha GUI opened. API and QuantGPT are healthy."
