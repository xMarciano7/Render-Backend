# app/main.py

import os
import uuid
import shutil
import base64
import requests
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse

RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY")
RUNPOD_ENDPOINT_ID = os.getenv("RUNPOD_ENDPOINT_ID")

if not RUNPOD_API_KEY or not RUNPOD_ENDPOINT_ID:
    raise RuntimeError("RUNPOD_API_KEY or RUNPOD_ENDPOINT_ID not set")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STORAGE_DIR = os.path.join(BASE_DIR, "storage")
INPUT_DIR = os.path.join(STORAGE_DIR, "input")
OUTPUT_DIR = os.path.join(STORAGE_DIR, "output")
PROGRESS_DIR = os.path.join(STORAGE_DIR, "progress")

for d in [INPUT_DIR, OUTPUT_DIR, PROGRESS_DIR]:
    os.makedirs(d, exist_ok=True)

app = FastAPI(
    title="Render Backend",
    docs_url="/docs",
    openapi_url="/openapi.json",
)

def write_progress(job_id: str, p: int):
    with open(os.path.join(PROGRESS_DIR, f"{job_id}.txt"), "w") as f:
        f.write(str(p))

def read_progress(job_id: str):
    try:
        return int(open(os.path.join(PROGRESS_DIR, f"{job_id}.txt")).read())
    except:
        return -1

@app.get("/")
def root():
    return {"status": "ok"}

@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    job_id = str(uuid.uuid4())
    input_path = os.path.join(INPUT_DIR, f"{job_id}.mp4")

    write_progress(job_id, 5)

    with open(input_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    # Enviar a RunPod
    with open(input_path, "rb") as f:
        video_b64 = base64.b64encode(f.read()).decode("utf-8")

    write_progress(job_id, 20)

    r = requests.post(
        f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/run",
        headers={
            "Authorization": f"Bearer {RUNPOD_API_KEY}",
            "Content-Type": "application/json",
        },
        json={"input": {"video_base64": video_b64}},
        timeout=60,
    )

    if r.status_code != 200:
        write_progress(job_id, -1)
        raise HTTPException(status_code=500, detail=r.text)

    runpod_job_id = r.json()["id"]
    with open(os.path.join(PROGRESS_DIR, f"{job_id}.runpod"), "w") as f:
        f.write(runpod_job_id)

    return {"job_id": job_id}

@app.get("/progress/{job_id}")
def progress(job_id: str):
    map_path = os.path.join(PROGRESS_DIR, f"{job_id}.runpod")
    if not os.path.exists(map_path):
        return {"percent": read_progress(job_id)}

    runpod_job_id = open(map_path).read().strip()

    r = requests.get(
        f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/status/{runpod_job_id}",
        headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"},
        timeout=10,
    )

    if r.status_code != 200:
        return {"percent": read_progress(job_id)}

    data = r.json()
    status = data.get("status")

    if status == "COMPLETED":
        output_b64 = data["output"]["video_base64"]
        video_bytes = base64.b64decode(output_b64)

        out_path = os.path.join(OUTPUT_DIR, f"{job_id}.mp4")
        with open(out_path, "wb") as f:
            f.write(video_bytes)

        write_progress(job_id, 100)
    elif status == "FAILED":
        write_progress(job_id, -1)
    else:
        write_progress(job_id, 50)

    return {"percent": read_progress(job_id)}

@app.get("/download/{job_id}")
def download(job_id: str):
    out_path = os.path.join(OUTPUT_DIR, f"{job_id}.mp4")
    if not os.path.exists(out_path):
        raise HTTPException(status_code=404, detail="Not ready")
    return FileResponse(out_path, media_type="video/mp4")
