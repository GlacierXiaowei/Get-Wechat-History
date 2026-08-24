@echo off
setlocal

where py >nul 2>&1
if errorlevel 1 goto try_python
py -3.10 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 goto try_python
py -3.10 "%~dp0plugin_bootstrap.py"
exit /b %errorlevel%

:try_python
where python >nul 2>&1
if errorlevel 1 goto missing_python
python "%~dp0plugin_bootstrap.py"
exit /b %errorlevel%

:missing_python
>&2 echo Get Wechat History requires Python 3.10 or newer. Install Python 3.10+ and retry.
exit /b 1
