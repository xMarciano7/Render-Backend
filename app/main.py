from fastapi import FastAPI, UploadFile, File
from fastapi.responses import JSONResponse
import requests
import base64
import os
import uuid

# =========================
# CONFIG
# =========================

RUNPOD_API_KEY = os.environ.get("RUNPOD_API_KEY")
RUNPOD_ENDPOINT_ID = os.environ.get("RUNPOD_ENDPOINT_ID")

RUNPOD_RUN_URL = f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/run"
RUNPOD_STATUS_URL = f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/status"

HEADERS = {
    "Authorization": f"Bearer {RUNPOD_API_KEY}",
    "Content-Type": "application/json",
}

# =========================
# FASTAPI APP
# =========================

app = FastAPI()

# =========================
# RUNPOD CLIENT
# =========================

def transcribe_with_runpod(audio_path: str):
    with open(audio_path, "rb") as f:
        audio_base64 = base64.b64encode(f.read()).decode("utf-8")

    payload = {
        "input": {
            "audio_base64": audio_base64
        }
    }

    r = requests.post(RUNPOD_RUN_URL, headers=HEADERS, json=payload, timeout=60)
    r.raise_for_status()
    job_id = r.json()["id"]

    # Polling
    while True:
        status = requests.get(
            f"{RUNPOD_STATUS_URL}/{job_id}",
            headers=HEADERS,
            timeout=30
        ).json()

        if status["status"] == "COMPLETED":
            return status["output"]

        if status["status"] == "FAILED":
            raise RuntimeError(status)

# =========================
# API ENDPOINT
# =========================

@app.post("/transcribe")
async def transcribe(file: UploadFile = File(...)):
    temp_path = f"/tmp/{uuid.uuid4()}_{file.filename}"

    with open(temp_path, "wb") as f:
        f.write(await file.read())

    try:
        result = transcribe_with_runpod(temp_path)
        return JSONResponse(content=result)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
