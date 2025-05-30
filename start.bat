@echo off
REM 自动安装依赖并启动交易机器人

REM 检查Python路径
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo 错误：请先将Python安装目录添加到系统PATH环境变量
    pause
    exit /b 1
)

REM 安装依赖库
pip install -r requirements.txt

REM 启动主程序
echo 正在启动Bitunix量化交易机器人...
python bitunix_trading_bot.py

pause