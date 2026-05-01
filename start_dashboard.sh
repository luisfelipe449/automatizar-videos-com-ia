#!/bin/bash
echo "Iniciando dashboard na porta 8000..."
python -m uvicorn app.web.main:app --host 127.0.0.1 --port 8000 --reload
