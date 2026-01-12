import os
import uuid
import json
import requests

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware


# =========================
# ENV
# =========================

RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY")
RUNPOD_ENDPOINT_ID = os.getenv("RUNPOD_ENDPOINT_ID")

if not RUNPOD_API_KEY or not RUNPOD_ENDPOINT_ID:
    raise RuntimeError("RUNPOD_API_KEY or RUNPOD_ENDPOINT_ID missing")

# =========================
# PATHS
# =========================

BASE = os.path.dirname(os.path.abspath(__file__))
STO = os.path.join(BASE, "storage")
PRO = os.path.join(STO, "progress")
URLS = os.path.join(STO, "urls")
PRESETS = os.path.join(STO, "presets")

os.makedirs(PRO, exist_ok=True)
os.makedirs(URLS, exist_ok=True)
os.makedirs(PRESETS, exist_ok=True)

# =========================
# APP
# =========================

app = FastAPI(
    title="ClipFile Backend",
    version="1.0",
    docs_url="/docs",
    openapi_url="/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://ucatml-my-site-teyd1jsn-othmanebenbrahim12.wix-vibe.com",
        "http://localhost:3000",
        "http://localhost:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================
# MODELS
# =========================

class UploadURL(BaseModel):
    youtube_url: str
    subtitle_preset: dict  # EXACTO lo que sale del frontend

# =========================
# PROGRESS HELPERS
# =========================

def write_progress(job_id: str, value: int):
    path = os.path.join(PRO, f"{job_id}.txt")
    try:
        current = int(open(path).read())
        if current >= value:
            return
    except:
        pass

    with open(path, "w") as f:
        f.write(str(value))


def read_progress(job_id: str) -> int:
    try:
        return int(open(os.path.join(PRO, f"{job_id}.txt")).read())
    except:
        return -1

# =========================
# ROUTES
# =========================

@app.post("/upload")
def upload_url(body: UploadURL):
    """
    1. Recibe URL + subtitle_preset del frontend
    2. Guarda preset EXACTO
    3. Lanza job en RunPod
    """
    job_id = str(uuid.uuid4())
    write_progress(job_id, 5)

    # Guardar preset EXACTO del usuario
    preset_path = os.path.join(PRESETS, f"{job_id}.json")
    with open(preset_path, "w", encoding="utf-8") as f:
        json.dump(body.subtitle_preset, f, ensure_ascii=False, indent=2)

    # Lanzar RunPod
    r = requests.post(
        f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/run",
        headers={
            "Authorization": f"Bearer {RUNPOD_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "input": {
                "job_id": job_id,
                "youtube_url": body.youtube_url,
                "subtitle_preset": body.subtitle_preset,  # PASA TODO
            }
        },
        timeout=20,
    )

    if r.status_code != 200:
        write_progress(job_id, -1)
        raise HTTPException(500, r.text)

    runpod_id = r.json().get("id")
    if not runpod_id:
        write_progress(job_id, -1)
        raise HTTPException(500, "RunPod did not return job id")

    # Guardar id de RunPod
    with open(os.path.join(PRO, f"{job_id}.runpod"), "w") as f:
        f.write(runpod_id)

    write_progress(job_id, 20)
    return {"job_id": job_id}


@app.get("/progress/{job_id}")
def progress(job_id: str):
    """
    Consulta estado del job en RunPod
    """
    current = read_progress(job_id)

    if current == 100 or current == -1:
        return {"percent": current}

    meta = os.path.join(PRO, f"{job_id}.runpod")
    if not os.path.exists(meta):
        return {"percent": current}

    runpod_id = open(meta).read().strip()

    r = requests.get(
        f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/status/{runpod_id}",
        headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"},
        timeout=10,
    )

    if r.status_code != 200:
        return {"percent": current}

    data = r.json()
    status = data.get("status")

    if status in ("SUCCEEDED", "COMPLETED", "COMPLETED_WITH_WARNINGS"):
        output = data.get("output") or {}
        youtube_url = output.get("youtube_url")

        if not youtube_url:
            write_progress(job_id, -1)
            return {"percent": -1}

        with open(os.path.join(URLS, f"{job_id}.txt"), "w") as f:
            f.write(youtube_url)

        write_progress(job_id, 100)

    elif status == "FAILED":
        write_progress(job_id, -1)

    else:
        # RUNNING
        write_progress(job_id, max(current, 50))

    return {"percent": read_progress(job_id)}


@app.get("/download/{job_id}")
def download(job_id: str):
    """
    Devuelve el clip final
    """
    path = os.path.join(URLS, f"{job_id}.txt")
    if not os.path.exists(path):
        raise HTTPException(404, "Not ready")

    return RedirectResponse(open(path).read().strip())
