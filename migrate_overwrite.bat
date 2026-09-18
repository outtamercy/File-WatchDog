@echo off
setlocal EnableDelayedExpansion

echo ========================================
echo   File Watchdog - Overwrite Migration
echo ========================================
echo.

:: --- Auto-detect MO2 root ---
set "SEARCH=%~dp0"
if "%SEARCH:~-1%"=="\" set "SEARCH=%SEARCH:~0,-1%"

:findloop
if exist "%SEARCH%\ModOrganizer.ini" (
    set "MO2_ROOT=%SEARCH%"
    goto found
)
for %%I in ("%SEARCH%\..") do set "PARENT=%%~fI"
if "%PARENT%"=="%SEARCH%" goto notfound
set "SEARCH=%PARENT%"
goto findloop

:notfound
echo ERROR: Could not find ModOrganizer.ini by searching up from:
echo   %~dp0
echo.
pause
exit /b 1

:found
echo MO2 Root:  %MO2_ROOT%

set "OVERWRITE=%MO2_ROOT%\overwrite"
set "MODS=%MO2_ROOT%\mods"

if not exist "%OVERWRITE%" (
    echo ERROR: Overwrite folder not found: %OVERWRITE%
    pause
    exit /b 1
)

if not exist "%MODS%" (
    echo ERROR: Mods folder not found: %MODS%
    pause
    exit /b 1
)

echo Overwrite: %OVERWRITE%
echo Mods:      %MODS%
echo.

:: --- Migrate each subfolder into its own -Cache mod ---
:: EXCEPT Root Builder's workspace folder: "Root"
set "TOTAL=0"
set "MIGRATED=0"

for /d %%D in ("%OVERWRITE%\*") do (
    set /a TOTAL+=1
    set "FOLDER_NAME=%%~nD"
    set "SRC=%%D"
    set "MOD_NAME=!FOLDER_NAME!-Cache"
    set "MOD_DIR=%MODS%\!MOD_NAME!"
    set "DEST=!MOD_DIR!\!FOLDER_NAME!"

    :: Skip Root Builder's private workspace
    if /I "!FOLDER_NAME!"=="Root" (
        echo [!FOLDER_NAME!] -^> SKIPPED (Root Builder workspace)
        goto :next_folder
    )

    echo [!FOLDER_NAME!] -^> [!MOD_NAME!]
    
    if not exist "!DEST!" mkdir "!DEST!"
    
    robocopy "!SRC!" "!DEST!" /E /MOVE /R:2 /W:1 >nul 2>&1
    
    rmdir "!SRC!" 2>nul
    
    if not exist "!SRC!" (
        mklink /J "!SRC!" "!DEST!" >nul 2>&1
        if !errorlevel! == 0 (
            echo   [OK] Migrated and junctioned
            set /a MIGRATED+=1
        ) else (
            echo   [FAIL] Could not create junction
        )
    ) else (
        echo   [WARN] Source still locked, skipping junction
    )

    :next_folder
)

:: --- Handle loose files at root of overwrite ---
echo.
echo Checking loose files...
set "MISC_MOD=%MODS%\Overwrite-Misc"
if not exist "%MISC_MOD%" mkdir "%MISC_MOD%"

for %%F in ("%OVERWRITE%\*") do (
    if exist "%%F" (
        set "FILE_NAME=%%~nxF"
        move "%%F" "%MISC_MOD%\!FILE_NAME!" >nul 2>&1
        if !errorlevel! == 0 (
            echo   [MOVED] !FILE_NAME! -^> Overwrite-Misc
        ) else (
            echo   [SKIP] !FILE_NAME! (locked or in use)
        )
    )
)

echo.
echo ========================================
echo   Migration complete.
echo   Folders scanned:  %TOTAL%
echo   Folders migrated: %MIGRATED%
echo   Loose files:      Overwrite-Misc
echo ========================================
echo.
echo Open MO2 and enable the new -Cache mods if they aren't already.
echo NOTE: If you use Root Builder, leave overwrite\Root alone.
pause