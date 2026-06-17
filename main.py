"""
FastAPI-Wrapper um analyze() für den Railway-Deploy von mixproof-audio-api.

multipart/form-data statt Base64-JSON, da FastAPI multipart nativ
unterstützt und der Nuxt-Proxy (mixproof-analyze.post.ts im Hauptrepo)
das hochgeladene File so 1:1 durchreichen kann.
"""

import asyncio
import io
import struct
import threading
import wave

from fastapi import FastAPI, File, UploadFile
from fastapi.responses import JSONResponse

from analyze import analyze

app = FastAPI()

_warmup_done = threading.Event()


def _silence_wav_bytes(seconds: float = 2.0, sr: int = 22050) -> bytes:
    n = int(sr * seconds)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(sr)
        f.writeframes(struct.pack("<" + "h" * n * 2, *([0] * n * 2)))
    return buf.getvalue()


def _run_warmup():
    try:
        analyze(_silence_wav_bytes())
        print("Warmup complete")
    except Exception as e:
        print(f"Warmup error (non-fatal): {e}")
    finally:
        _warmup_done.set()


@app.on_event("startup")
async def warmup():
    threading.Thread(target=_run_warmup, daemon=True).start()


@app.get("/")
@app.get("/health")
def health():
    return {"status": "ok", "v": "b67fa85"}


@app.post("/analyze")
async def analyze_endpoint(audio: UploadFile = File(...)):
    # Wait for warmup to finish (max 90s) before processing real audio
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: _warmup_done.wait(timeout=90))

    audio_bytes = await audio.read()
    try:
        result = analyze(audio_bytes)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)
    return result
