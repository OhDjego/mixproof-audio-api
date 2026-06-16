"""
FastAPI-Wrapper um analyze() für den Railway-Deploy von mixproof-audio-api.

multipart/form-data statt Base64-JSON, da FastAPI multipart nativ
unterstützt und der Nuxt-Proxy (mixproof-analyze.post.ts im Hauptrepo)
das hochgeladene File so 1:1 durchreichen kann.
"""

import io
import struct
import threading
import wave

from fastapi import FastAPI, File, UploadFile
from fastapi.responses import JSONResponse

from analyze import analyze

app = FastAPI()


def _silence_wav_bytes(seconds: float = 1.0, sr: int = 22050) -> bytes:
    n = int(sr * seconds)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sr)
        f.writeframes(struct.pack("<" + "h" * n, *([0] * n)))
    return buf.getvalue()


def _warmup_thread() -> None:
    """Numba-JIT-Kompilierung im Hintergrund — blockiert nicht den Health-Check."""
    try:
        analyze(_silence_wav_bytes())
    except Exception:
        pass


@app.on_event("startup")
def warmup() -> None:
    threading.Thread(target=_warmup_thread, daemon=True).start()


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
