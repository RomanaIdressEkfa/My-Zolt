' Starts Zolt's watchdog without a console window. A shortcut to this file lives in the
' Windows Startup folder, so Zolt starts at login and restarts itself if it crashes.
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = here
shell.Run "pythonw.exe """ & here & "\run_forever.py""", 0, False
