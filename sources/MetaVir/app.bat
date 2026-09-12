@echo off
echo Starting MetaVir Server...
start "" http://localhost:5000
if exist venv\Scripts\python.exe (
    venv\Scripts\python.exe app.py
) else (
    python app.py
)
