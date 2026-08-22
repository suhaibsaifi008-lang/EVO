Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
pyw = dir & "\.venv\Scripts\pythonw.exe"

If Not fso.FileExists(pyw) Then
  MsgBox "Run scripts\start.bat once first so EVO can install itself.", 48, "EVO"
  WScript.Quit
End If

sh.CurrentDirectory = dir
sh.Run """" & pyw & """ -m uvicorn main:app --host 127.0.0.1 --port 8420", 0, False
WScript.Sleep 2500
sh.Run "http://localhost:8420"
