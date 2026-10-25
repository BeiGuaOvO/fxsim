@echo off
chcp 65001 >nul
cd /d %~dp0

if not exist venv (
  echo [setup] 创建虚拟环境...
  python -m venv venv
  call venv\Scripts\activate.bat
  python -m pip install -U pip
  pip install -r requirements.txt
) else (
  call venv\Scripts\activate.bat
)

set FX_TICK_SECONDS=1
set FX_START_BALANCE=300000
set FX_GOAL=20000000
set FX_LEVERAGES=1,5,10,25,50,75,100
set FX_MIN_AMOUNT=1000
set FX_MAX_AMOUNT=10000000
set FX_CANDLE_TICKS=5
set FX_NEWS=1
set FX_NEWS_MIN=90
set FX_NEWS_MAX=240
set FX_NEWS_GAP=0.35
set FX_STOPOUT=50
set FX_WARN=130
set FX_TOPUP_TARGET=160
set FX_MAX_DEPOSIT=1000000

echo.
echo   游戏地址: http://127.0.0.1:8000
echo   关闭此窗口即停止服务器
echo.
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
pause