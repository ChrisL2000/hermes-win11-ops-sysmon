@echo off
rem One-shot status: current run-state, transition progress, gates, head.
setlocal
cd /d "%~dp0"
".venv\Scripts\python.exe" -c "import json,urllib.request;d=json.load(urllib.request.urlopen('http://127.0.0.1:8765/api/state'));c=d['current'];t=d['transition'] or {};print('runstate :',c.get('runstate'));print('busy     :',d['busy']);print('last     :',t.get('state'),t.get('to'),str(t.get('progress'))+'%');print('drift    :',len(d.get('drift') or []))"
echo.
".venv\Scripts\python.exe" -m sysmon gates
