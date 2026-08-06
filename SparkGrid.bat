@echo off
setlocal

set "INSTALL_ROOT=%~dp0"
if "%INSTALL_ROOT:~-1%"=="\" set "INSTALL_ROOT=%INSTALL_ROOT:~0,-1%"

set "INTERNAL_DIR=%INSTALL_ROOT%\_internal"
set "PYTHON_DIR=%INSTALL_ROOT%\python"
set "RUNTIME_LIB=%LOCALAPPDATA%\SparkGrid\lib"

set "SPARKGRID_DATA_DIR=%LOCALAPPDATA%\SparkGrid\data"
set "SPARKGRID_CAMOUFOX_DIR=%INTERNAL_DIR%\SparkBrowser"
set "SPARKGRID_GEOIP_PATH=%INTERNAL_DIR%\camoufox\GeoLite2-City.mmdb"
set "PYTHONPATH=%PYTHON_DIR%\Lib\site-packages;%INTERNAL_DIR%;%RUNTIME_LIB%;%INSTALL_ROOT%\lib"
set "WEB_UI_HOST=127.0.0.1"
set "WEB_UI_PORT=8770"

if not exist "%SPARKGRID_DATA_DIR%" mkdir "%SPARKGRID_DATA_DIR%"

set "PYTHON_EXE=%PYTHON_DIR%\python.exe"

echo ============================================================
echo  SparkGrid Instagram Web Upload v2.20.14-beta.1
echo  Server: http://%WEB_UI_HOST%:%WEB_UI_PORT%
echo  Data:   %SPARKGRID_DATA_DIR%
echo ============================================================

start "" "http://%WEB_UI_HOST%:%WEB_UI_PORT%"

"%PYTHON_EXE%" "%INTERNAL_DIR%\app.py"

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Server exited with code %ERRORLEVEL%
    pause
)

endlocal
