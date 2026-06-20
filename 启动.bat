@echo off
chcp 65001 >nul
title AI 软件开发工厂 - 一键启动

echo ==========================================
echo   AI 软件开发工厂 V2 - 启动中...
echo ==========================================
echo.

:: 启动后端
echo [1/2] 启动后端服务 (localhost:8000)...
start "后端服务" cmd /k "cd /d %~dp0backend && python run.py"

:: 等待后端启动
timeout /t 3 /nobreak >nul

:: 启动前端
echo [2/2] 启动前端服务 (localhost:5173)...
start "前端服务" cmd /k "cd /d %~dp0frontend && npm run dev"

echo.
echo ==========================================
echo   启动完成！
echo   前端: http://localhost:5173
echo   后端: http://localhost:8000
echo   关闭此窗口不会影响服务
echo ==========================================
echo.
pause
