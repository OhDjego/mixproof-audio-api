# mixproof-audio-api

librosa-basierte Audio-Analyse als FastAPI-Service für [Mixproof](https://github.com/OhDjego/Mixproof).

Liefert BPM, Tonart (Key), LUFS, LRA, True Peak, Crest Factor, DR, Stereo
Width und Mono-Kompatibilität für hochgeladene Audiodateien.

## Endpoints

- `GET /` — Health-Check, gibt `{"status": "ok"}` zurück.
- `POST /analyze` — `multipart/form-data` mit Feld `audio` (Datei), gibt
  alle Metriken als JSON zurück.

## Lokal ausführen

```bash
pip install -r requirements.txt fastapi uvicorn python-multipart
uvicorn main:app --host 0.0.0.0 --port 8000
```

## Deployment (Railway)

Dieses Repo enthält ein `Dockerfile` und eine `railway.toml`. Railway
erkennt beides automatisch, Build und Start laufen ohne weitere
Konfiguration.

Die Mixproof-Hauptanwendung erwartet die deployte URL in der Env-Var
`PYTHON_API_URL`.
