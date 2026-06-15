"""
MIXPROOF — librosa-basierte Audio-Analyse (Vercel Python Serverless Function).

Entscheidung (keine Rückfrage nötig, siehe CLAUDE.md):
- Vercel Python Runtime im "BaseHTTPRequestHandler"-Stil (keine zusätzlichen
  Web-Frameworks wie Flask/FastAPI nötig -> kleinerer Funktions-Bundle).
- Eingabe: POST JSON { "audio": "<base64>", "filename": "..." }. Base64 statt
  multipart, weil Vercel Python Functions kein natives multipart-Parsing haben
  und die Nuxt-Seite (server/api/mixproof-analyze.post.ts) das Audio ohnehin
  schon als Buffer vorliegen hat.
- Ausgabe: JSON mit BPM (librosa Beat-Tracking + Tempogramm-Konfidenz),
  Tonart (Chroma + Krumhansl-Schmuckler-Korrelation, alle 12 Rotationen,
  Major/Minor), Spectral Centroid, Spectral Rolloff, Zero-Crossing-Rate,
  Onset-Stärke-Statistik sowie die vollständigen Loudness-/Stereo-Metriken
  (Integrated LUFS, LRA, True Peak, Crest Factor, DR, Stereo Width, Mono-
  Kompatibilität). Diese Python-Werte sind jetzt die primäre Quelle für
  mixproof-logic.js; die bestehende BS.1770-Implementierung in JS bleibt nur
  als Fallback, falls diese Function nicht erreichbar ist (z.B. `nuxt dev`
  ohne `vercel dev`).
- Stereo-Dekodierung via soundfile (sf.read, always_2d) statt librosa.load,
  damit L/R für LUFS/LRA/True-Peak/Stereo-Metriken erhalten bleiben;
  librosa.to_mono() liefert daraus das Mono-Signal für BPM/Key/Spektral-
  Features. sr=None / native Samplerate (kein Resampling-Overhead).
- LUFS/LRA via pyloudnorm (BS.1770-4 K-Weighting + Gating ist dort korrekt
  und gut getestet implementiert -> spart eine fehleranfällige Eigen-
  implementierung im Serverless-Kontext). True Peak/DR/Stereo-Width/Mono-
  Kompat sind eigene, dokumentierte Implementierungen (siehe unten).
"""

from http.server import BaseHTTPRequestHandler
import json
import base64
import io

import numpy as np
import librosa
import soundfile as sf
import pyloudnorm as pyln
from scipy.signal import resample_poly

# Krumhansl-Schmuckler 1990 Tonart-Profile (gleiche Werte wie in
# public/mixproof-logic.js, damit beide Analysen vergleichbar sind)
KS_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
KS_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']


def estimate_key(y, sr):
    """Chroma-Mittel über den ganzen Track, Korrelation gegen alle 24
    KS-Rotationen (12 Dur + 12 Moll) -> bestes Match + Konfidenz (0-1)."""
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
    chroma_mean = chroma.mean(axis=1)
    if chroma_mean.std() > 0:
        chroma_norm = (chroma_mean - chroma_mean.mean()) / chroma_mean.std()
    else:
        chroma_norm = chroma_mean

    best = {'corr': -2.0, 'key': 'C', 'mode': 'major'}
    scores = []
    for mode, profile in (('major', KS_MAJOR), ('minor', KS_MINOR)):
        p_norm = (profile - profile.mean()) / profile.std()
        for shift in range(12):
            rotated = np.roll(p_norm, shift)
            corr = float(np.corrcoef(chroma_norm, rotated)[0, 1])
            scores.append(corr)
            if corr > best['corr']:
                best = {'corr': corr, 'key': NOTE_NAMES[shift], 'mode': mode}

    scores_sorted = sorted(scores, reverse=True)
    top, second = scores_sorted[0], scores_sorted[1] if len(scores_sorted) > 1 else scores_sorted[0]
    # Konfidenz: normalisierter Abstand zwischen bestem und zweitbestem Match
    confidence = float(np.nan_to_num(np.clip((top - second) / 2, 0, 1), nan=0.0))

    return {
        'key': best['key'],
        'mode': best['mode'],
        'confidence': round(confidence, 3),
    }


def estimate_bpm(y, sr):
    """librosa Beat-Tracking + Tempogramm-basierte Konfidenz.
    Konfidenz = wie dominant der Haupt-Tempo-Peak im Tempogramm gegenüber
    dem Median ist (0-1, geclamped)."""
    onset_env = librosa.onset.onset_strength(y=y, sr=sr)
    tempo, beats = librosa.beat.beat_track(onset_envelope=onset_env, sr=sr)
    bpm = float(tempo if np.ndim(tempo) == 0 else tempo[0])

    tempogram = librosa.feature.tempogram(onset_envelope=onset_env, sr=sr)
    tg_mean = tempogram.mean(axis=1)
    peak = float(tg_mean.max())
    median = float(np.median(tg_mean)) or 1e-9
    confidence = float(np.clip((peak / median - 1) / 4, 0, 1))

    return {
        'bpm': round(bpm, 1),
        'confidence': round(confidence, 3),
        'beatCount': int(len(beats)),
    }


def compute_true_peak_db(data):
    """True Peak (dBTP), BS.1770-4 Annex 2 Näherung: 4x-Oversampling pro
    Kanal (polyphasen-Resampling), dann Maximum des Betrags über alle
    Kanäle. Analog zur 8x-Catmull-Rom-Interpolation in truePeak() (JS),
    aber via scipy.signal.resample_poly."""
    peak = 0.0
    for ch in range(data.shape[1]):
        oversampled = resample_poly(data[:, ch], up=4, down=1)
        ch_peak = float(np.max(np.abs(oversampled)))
        peak = max(peak, ch_peak)
    return round(20 * np.log10(peak), 2) if peak > 0 else -144.0


def compute_lra(data, sr, meter):
    """Loudness Range (EBU R128 / Tech 3342): Short-Term-Loudness in
    3s-Blöcken mit 1s-Hop, absolutes Gate bei -70 LUFS, relatives Gate bei
    -20 LU unterhalb des ungegateten Mittelwerts, LRA = P95 - P10 der
    verbleibenden Werte."""
    block, hop = int(sr * 3), int(sr * 1)
    n = data.shape[0]
    if n < block:
        return 0.0
    values = []
    for start in range(0, n - block + 1, hop):
        try:
            l = meter.integrated_loudness(data[start:start + block])
        except Exception:
            continue
        if np.isfinite(l) and l >= -70:
            values.append(l)
    if len(values) < 4:
        return 0.0
    values = np.array(values)
    rel = values[values >= values.mean() - 20]
    if len(rel) < 2:
        rel = values
    return round(float(np.percentile(rel, 95) - np.percentile(rel, 10)), 2)


def compute_dr(data, sr):
    """DR (TT-DR-Meter-Algorithmus, vereinfacht): 3s-Blöcke pro Kanal,
    Block-RMS = sqrt(2*mean(x^2)), die lautesten 20% der Blöcke verwerfen,
    DR = 20*log10(Peak / Mittelwert der restlichen Block-RMS), gemittelt
    über die Kanäle."""
    block = int(sr * 3)
    n = data.shape[0]
    if n < block:
        block = n
    dr_values = []
    for ch in range(data.shape[1]):
        x = data[:, ch]
        peak = float(np.max(np.abs(x)))
        if peak <= 0 or block <= 0:
            continue
        rms_blocks = []
        for start in range(0, n - block + 1, block):
            seg = x[start:start + block]
            rms = np.sqrt(2 * np.mean(seg ** 2))
            if rms > 0:
                rms_blocks.append(rms)
        if len(rms_blocks) < 2:
            continue
        rms_blocks.sort(reverse=True)
        discard = max(1, int(len(rms_blocks) * 0.2))
        rest = rms_blocks[discard:] or rms_blocks
        dr_values.append(20 * np.log10(peak / np.mean(rest)))
    return round(float(np.mean(dr_values)), 1) if dr_values else 0.0


def compute_crest_factor_db(data, true_peak_db):
    """Crest Factor (dB) = True Peak - RMS, analog zu calcCrestFactor() in
    mixproof-logic.js (RMS über L+R kombiniert)."""
    rms = np.sqrt(np.mean(data.astype(np.float64) ** 2))
    rms_db = 20 * np.log10(rms) if rms > 0 else -144.0
    return round(true_peak_db - rms_db, 1)


def compute_stereo_metrics(data):
    """Stereo Width = RMS(Side)/RMS(Mid), wie calcWidth() in
    mixproof-logic.js. Mono-Kompatibilität = Pearson-Korrelation L/R
    (-1..1), wie calcPhase() in mixproof-logic.js (1 = identisch/mono-fest,
    -1 = vollständig gegenphasig -> löscht sich beim Mono-Summieren aus)."""
    if data.shape[1] < 2:
        return {'stereoWidth': 0.0, 'monoCompatibility': 1.0}
    L, R = data[:, 0], data[:, 1]
    mid, side = (L + R) * 0.5, (L - R) * 0.5
    rm, rs = np.sqrt(np.mean(mid ** 2)), np.sqrt(np.mean(side ** 2))
    width = round(float(rs / rm), 2) if rm > 0 else 0.0
    den = np.sqrt(np.sum(L * L) * np.sum(R * R)) + 1e-12
    corr = round(float(np.sum(L * R) / den), 2)
    return {'stereoWidth': width, 'monoCompatibility': corr}


def analyze(audio_bytes):
    data, sr = sf.read(io.BytesIO(audio_bytes), always_2d=True, dtype='float32')
    if data.shape[1] == 1:
        data = np.repeat(data, 2, axis=1)
    y = librosa.to_mono(data.T)

    bpm_result = estimate_bpm(y, sr)
    key_result = estimate_key(y, sr)

    spectral_centroid = float(librosa.feature.spectral_centroid(y=y, sr=sr).mean())
    spectral_rolloff = float(librosa.feature.spectral_rolloff(y=y, sr=sr).mean())
    zcr = float(librosa.feature.zero_crossing_rate(y=y).mean())
    onset_env = librosa.onset.onset_strength(y=y, sr=sr)

    meter = pyln.Meter(sr)
    try:
        lufs_integrated = float(meter.integrated_loudness(data))
    except Exception:
        lufs_integrated = -70.0
    if not np.isfinite(lufs_integrated):
        lufs_integrated = -70.0

    true_peak_db = compute_true_peak_db(data)

    return {
        'sr': int(sr),
        'durationSec': round(float(len(y) / sr), 2),
        'channels': int(data.shape[1]),
        'bpm': bpm_result,
        'key': key_result,
        'spectralCentroidHz': round(spectral_centroid, 1),
        'spectralRolloffHz': round(spectral_rolloff, 1),
        'zeroCrossingRate': round(zcr, 4),
        'onsetStrengthMean': round(float(onset_env.mean()), 4),
        'lufsIntegrated': round(lufs_integrated, 2),
        'lra': compute_lra(data, sr, meter),
        'truePeakDb': true_peak_db,
        'crestFactorDb': compute_crest_factor_db(data, true_peak_db),
        'dr': compute_dr(data, sr),
        **compute_stereo_metrics(data),
    }


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode('utf-8'))

            audio_b64 = payload.get('audio')
            if not audio_b64:
                self._send_json({'error': 'Missing "audio" (base64) in request body.'}, status=400)
                return

            audio_bytes = base64.b64decode(audio_b64)
            result = analyze(audio_bytes)
            self._send_json(result, status=200)
        except Exception as e:  # noqa: BLE001 - serverless boundary, report error to caller
            self._send_json({'error': str(e)}, status=500)

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
