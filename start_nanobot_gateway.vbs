Set WshShell = CreateObject("WScript.Shell")
Set FSO = CreateObject("Scripting.FileSystemObject")

scriptDir = FSO.GetParentFolderName(WScript.ScriptFullName)
pythonPath = FSO.BuildPath(scriptDir, ".venv\Scripts\python.exe")
logPath = FSO.BuildPath(scriptDir, "gateway.boot.log")
errPath = FSO.BuildPath(scriptDir, "gateway.boot.err.log")

WshShell.CurrentDirectory = scriptDir

If Not FSO.FileExists(pythonPath) Then
  WScript.Echo "Python executable not found: " & pythonPath
  WScript.Quit 1
End If

cmd = "cmd /c cd /d """ & scriptDir & """ && """ & pythonPath & _
  """ -m nanobot gateway >> """ & logPath & """ 2>> """ & errPath & """"
WshShell.Run cmd, 0, False
