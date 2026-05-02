@echo off
echo ========================================
echo DLsite Crawler - Full Pipeline
echo ========================================
echo.

echo [1/3] Running crawler...
python crawler.py
if errorlevel 1 (
    echo Crawler failed!
    pause
    exit /b 1
)

echo.
echo [2/3] Generating web data...
python generate.py
if errorlevel 1 (
    echo Generate failed!
    pause
    exit /b 1
)

echo.
echo [3/3] Importing translations...
python md_to_json.py
if errorlevel 1 (
    echo Import failed!
    pause
    exit /b 1
)

echo.
echo ========================================
echo All done!
echo ========================================
pause
