#!/bin/bash
# Télécharge camoufox en arrière-plan, démarre l'API immédiatement
python -m camoufox fetch &
exec uvicorn main:app --host 0.0.0.0 --port 8000
