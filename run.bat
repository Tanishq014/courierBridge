@echo off
echo Starting CourierBridge Setup...

IF NOT EXIST "venv" (
    echo Creating virtual environment...
    python -m venv venv
)

echo Activating virtual environment...
call venv\Scripts\activate.bat

echo Installing dependencies...
pip install -r requirements.txt

echo Starting CourierBridge Server on port 8001...
uvicorn app.main:app --reload --port 8001

pause
