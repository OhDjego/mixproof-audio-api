"""
FastAPI-Wrapper um analyze() für den Railway-Deploy von mixproof-audio-api.

multipart/form-data statt Base64-JSON, da FastAPI multipart nativ
unterstützt und der Nuxt-Proxy (mixproof-analyze.post.ts im Hauptrepo)
das hochgeladene File so 1:1 durchreichen kann.
"""

import threading

from fastapi import FastAPI, File, UploadFile
from fastapi.responses import JSONResponse

from analyze import analyze

app = FastAPI()


def _run_warmup():
    import numpy as np
    import librosa
    dummy = np.zeros(22050, dtype=np.float32)
    librosa.beat.beat_track(y=dummy, sr=22050)
    print("Warmup complete")


@app.on_event("startup")
async def warmup():
    threading.Thread(target=_run_warmup, daemon=True).start()


@app.get("/")
@app.get("/health")
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
