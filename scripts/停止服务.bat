@echo off
chcp 65001 >nul
echo 停止 xianyu-agent 相关 Python 进程...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'xianyu-agent' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host ('已停止 PID ' + $_.ProcessId) }"
echo 完成。
pause
