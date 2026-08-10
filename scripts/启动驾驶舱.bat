@echo off
chcp 65001 >nul
cd /d "%~dp0\.."
echo 启动 xianyu-agent 驾驶舱(q 退出,! 紧急模式)...
uv run xianyu-agent dashboard
pause
