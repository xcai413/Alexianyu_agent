@echo off
chcp 65001 >nul
cd /d "%~dp0\.."
echo 启动 xianyu-agent MCP Server(SSE)...
uv run xianyu-agent mcp serve
pause
