#!/usr/bin/env bash
# run.sh – Startskript für RAG-Demo (FastAPI + Uvicorn)

set -e

# In das Projektverzeichnis wechseln
cd "$(dirname "$0")"

# Prüfen ob venv existiert, ansonsten anlegen
if [ ! -d ".venv" ]; then
  echo "[INFO] Virtuelle Umgebung wird erstellt..."
  python3 -m venv .venv
fi

# Aktivieren
source .venv/bin/activate

# Pip aktualisieren
python -m pip install --upgrade pip

# Abhängigkeiten installieren
if [ -f requirements-min.txt ]; then
  echo "[INFO] Installiere requirements-min.txt (für Step 1)"
  pip install -r requirements-min.txt
else
  pip install -r requirements.txt
fi

# Server starten
echo "[INFO] Starte Uvicorn auf http://127.0.0.1:8000"

# start with auto-reload for development
#exec uvicorn app.main:app --reload

# start without auto-reload for production
exec uvicorn app.main:app