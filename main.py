"""
FastAPI-Wrapper um analyze() für das Railway-Deployment.

Self-contained Verzeichnis (railway/), damit Railway dieses Unterverzeichnis
als Service-Root verwenden kann (eigenes Dockerfile, railway.toml,
requirements.txt, analyze.py). analyze.py ist eine Kopie von
api/analyze.py (Vercel-Function, separat, siehe vercel.json) - die
Analyse-Logik selbst ist identisch, nur der Transport unterscheidet sich:
hier multipart/form-data statt Base64-JSON, da FastAPI multipart nativ
unterstützt und die Nuxt-Seite (server/api/mixproof-analyze.post.ts) das
hochgeladene File so 1:1 durchreichen kann.
"""

from fastapi import FastAPI, File, UploadFile
from fastapi.responses import JSONResponse

from analyze import analyze

app = FastAPI()


@app.get("/")
def health():
    return {"status": "ok"}


@app.post("/analyze")
async def analyze_endpoint(audio: UploadFile = File(...)):
    audio_bytes = await audio.read()
    try:
        result = analyze(audio_bytes)
    except Exception as e:  # noqa: BLE001 - HTTP-Boundary, Fehler an Caller melden
        return JSONResponse({"error": str(e)}, status_code=500)
    return result
