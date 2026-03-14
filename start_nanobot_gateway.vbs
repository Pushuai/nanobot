Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "D:\code\nanobot"
cmd = "cmd /c ""D:\code\nanobot\.venv\Scripts\python.exe -m nanobot gateway >> D:\code\nanobot\gateway.boot.log 2>&1"""
WshShell.Run cmd, 0, False
