# M-Bull env diagnosis (ASCII only).
# IMPORTANT: run this in the SAME terminal window you use to start the tool,
# then send ALL output back to the AI.
Write-Host "=== 1. current python ==="
$py = Get-Command python -ErrorAction SilentlyContinue
if ($py) {
    Write-Host ("python path: " + $py.Source)
    python --version
    python -c "import sys; print('sys.executable =', sys.executable)"
    Write-Host "--- pytdx check ---"
    python -c "import pytdx; print('pytdx OK')"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "pytdx missing -> installing ..."
        python -m pip install --no-cache-dir pytdx==1.72
        python -c "import pytdx; print('pytdx installed OK')"
    }
} else {
    Write-Host "NO python in this terminal PATH." -ForegroundColor Yellow
    Write-Host "If you start the tool via a venv: activate it first, then re-run this script." -ForegroundColor Yellow
}

Write-Host "=== 2. tdx nodes connectivity (7709) ==="
foreach ($ip in @('117.34.114.15','117.34.114.14','117.34.114.20')) {
    $r = Test-NetConnection -ComputerName $ip -Port 7709 -WarningAction SilentlyContinue
    Write-Host ("  {0}:7709 -> TcpOk={1}" -f $ip, $r.TcpTestSucceeded)
}

Write-Host "=== 3. M-Bull.exe search on C/D/E (may take 1-2 min) ==="
foreach ($drive in @('C:\','D:\','E:\')) {
    Get-ChildItem -Path $drive -Filter "M-Bull.exe" -Recurse -Depth 4 -ErrorAction SilentlyContinue |
        Select-Object -First 5 FullName, LastWriteTime | Format-Table -AutoSize
}

Write-Host "=== DONE. Send ALL output back to the AI. ==="
