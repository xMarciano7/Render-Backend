# app/main.py

import os
import uuid
import shutil
import requests
from fastapi import FastAPI, UploadFile, File, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse, JSONResponse

RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY")
RUNPOD_ENDPOINT_ID = os.getenv("RUNPOD_ENDPOINT_ID")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STORAGE_DIR = os.path.join(BASE_DIR, "storage")
INPUT_DIR = os.path.join(STORAGE_DIR, "input")
OUTPUT_DIR = os.path.join(STORAGE_DIR, "output")
PROGRESS_DIR = os.path.join(STORAGE_DIR, "progress")

os.makedirs(INPUT_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(PROGRESS_DIR, exist_ok=True)

app = FastAPI(
    title="Render Backend",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json"
)



def run_transcription(job_id: str, input_path: str):
    progress_path = os.path.join(PROGRESS_DIR, f"{job_id}.txt")
    output_path = os.path.join(OUTPUT_DIR, f"{job_id}.json")

    def write_progress(p):
        with open(progress_path, "w") as f:
            f.write(str(p))

    write_progress(5)

    with open(input_path, "rb") as f:
        audio_bytes = f.read()

    import base64
    audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")

    write_progress(20)

    resp = requests.post(
        f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/run",
        headers={
            "Authorization": f"Bearer {RUNPOD_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "input": {
                "audio_base64": audio_b64
            }
        },
        timeout=300,
    )

    if resp.status_code != 200:
        write_progress(-1)
        return

    job = resp.json()
    runpod_job_id = job["id"]

    write_progress(40)

    while True:
        status_resp = requests.get(
            f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/status/{runpod_job_id}",
            headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"},
        )
        data = status_resp.json()

        if data["status"] == "COMPLETED":
            with open(output_path, "w") as f:
                f.write(str(data["output"]))
            write_progress(100)
            break

        if data["status"] == "FAILED":
            write_progress(-1)
            break


@app.get("/")
def root():
    return {"status": "ok"}


@app.post("/upload")
def upload_video(file: UploadFile = File(...), background_tasks: BackgroundTasks = None):
    job_id = str(uuid.uuid4())
    input_path = os.path.join(INPUT_DIR, f"{job_id}.wav")

    with open(input_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    progress_path = os.path.join(PROGRESS_DIR, f"{job_id}.txt")
    with open(progress_path, "w") as f:
        f.write("0")

    background_tasks.add_task(run_transcription, job_id, input_path)

    return {"job_id": job_id}


@app.get("/progress/{job_id}")
def progress(job_id: str):
    progress_path = os.path.join(PROGRESS_DIR, f"{job_id}.txt")
    if not os.path.exists(progress_path):
        raise HTTPException(status_code=404, detail="Job not found")

    with open(progress_path) as f:
        p = f.read()

    return {"progress": p}


@app.get("/download/{job_id}")
def download(job_id: str):
    output_path = os.path.join(OUTPUT_DIR, f"{job_id}.json")
    if not os.path.exists(output_path):
        raise HTTPException(status_code=404, detail="Not ready")

    return FileResponse(output_path, media_type="application/json", filename=f"{job_id}.json")
