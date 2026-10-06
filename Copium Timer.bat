@echo off
rem Launches the Green Timer window + the Copium timer terminal without an extra console.
where pyw >nul 2>nul && (
    start "" pyw -3 "%~dp0copium_timer.py"
) || (
    start "" pythonw "%~dp0copium_timer.py"
)
