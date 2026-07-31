@echo off
REM Run ONLY the expense-reimbursement module locally, on its own DB (data\exp.db)
REM and port 8021 — fully separate from the org tree (run.bat, port 8020).
set ARKIA_DB_PATH=data\exp.db
python -m uvicorn app.exp_app:app --port 8021 --reload
