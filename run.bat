@echo off
python -m uvicorn app.main:app --port 8020 --reload
