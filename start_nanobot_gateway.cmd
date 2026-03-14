@echo off
setlocal

set "ROOT=%~dp0"
set "VBS=%ROOT%start_nanobot_gateway.vbs"

if not exist "%VBS%" (
  echo VBS launcher not found: "%VBS%"
  exit /b 1
)

wscript.exe "%VBS%"
