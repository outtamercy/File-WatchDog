@echo off
setlocal EnableDelayedExpansion

echo ========================================
echo   File Watchdog - Remove / Restore
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
echo ERROR: Could not find ModOrganizer.ini
echo Place this script inside your MO2 tree.
pause
exit /b 1

:found
echo MO2 Root:  %MO2_ROOT%
set "OVERWRITE=%MO2_ROOT%\overwrite"
set "MODS=%MO2_ROOT%\mods"

if not exist "%OVERWRITE%" (
    echo ERROR: Overwrite folder not found
    pause
    exit /b 1
)

:: --- Remove junctions and restore files ---
echo.
echo Scanning overwrite for File Watchdog junctions...

set "RESTORED=0"
set "SKIPPED=0"

for /d %%D in ("%OVERWRITE%\*") do (
    set "NAME=%%~nD"
    set "JUNCTION=%%D"

    :: Check if this folder is a reparse point (junction/symlink)
    fsutil reparsepoint query "!JUNCTION!" >nul 2>&1
    if !errorlevel! == 0 (
        echo [!NAME!] Removing junction...

        :: Delete the junction (only removes the link, not the target)
        rmdir "!JUNCTION!" 2>nul

        :: Try to restore from -Redirect first (new naming)
        set "SOURCE=%MODS%\!NAME!-Redirect\!NAME!"
        if exist "!SOURCE!" (
            mkdir "!JUNCTION!" 2>nul
            robocopy "!SOURCE!" "!JUNCTION!" /E /MOVE /R:2 /W:1 >nul 2>&1
            rmdir "!SOURCE!" 2>nul
            rmdir "%MODS%\!NAME!-Redirect" 2>nul
            set /a RESTORED+=1
            echo   [OK] Restored from !NAME!-Redirect
        ) else (
            :: Fall back to legacy -Cache naming
            set "SOURCE=%MODS%\!NAME!-Cache\!NAME!"
            if exist "!SOURCE!" (
                mkdir "!JUNCTION!" 2>nul
                robocopy "!SOURCE!" "!JUNCTION!" /E /MOVE /R:2 /W:1 >nul 2>&1
                rmdir "!SOURCE!" 2>nul
                rmdir "%MODS%\!NAME!-Cache" 2>nul
                set /a RESTORED+=1
                echo   [OK] Restored from !NAME!-Cache
            ) else (
                echo   [WARN] Source files missing for !NAME!
                set /a SKIPPED+=1
            )
        )
    )
)

:: --- Restore loose files from Overwrite-Misc ---
echo.
echo Checking for loose files in Overwrite-Misc...
set "MISC=%MODS%\Overwrite-Misc"
if exist "%MISC%" (
    for %%F in ("%MISC%\*") do (
        if exist "%%F" (
            set "FNAME=%%~nxF"
            move "%%F" "%OVERWRITE%\!FNAME!" >nul 2>&1
            if !errorlevel! == 0 (
                echo   [RESTORED] !FNAME!
                set /a RESTORED+=1
            ) else (
                echo   [SKIP] !FNAME! (file locked)
            )
        )
    )
    rmdir "%MISC%" 2>nul
)

:: --- Clean up leftover .bak folders from plugin runs ---
echo.
echo Cleaning up leftover .bak folders...
for /d %%B in ("%OVERWRITE%\*.bak") do (
    set "BAKNAME=%%~nB"
    echo   [REMOVED] %%~nxB
    rmdir /s /q "%%B" 2>nul
    set /a RESTORED+=1
)

echo.
echo ========================================
echo   File Watchdog removed.
echo   Folders restored: %RESTORED%
echo   Skipped/missing:   %SKIPPED%
echo ========================================
echo.
echo You can now delete the File Watchdog plugin folder:
echo   %MO2_ROOT%\plugins\file_watchdog
echo.
pause