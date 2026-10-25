@echo off
rem Console launcher: shows logs, useful for troubleshooting.
rem Extra options: --update-now  --rollback  --clear-skipped  --version  --no-update
cd /d "%~dp0"
"%~dp0python\python.exe" -E -s "%~dp0launcher.pyw" %*
if errorlevel 1 pause
