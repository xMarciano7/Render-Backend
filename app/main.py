from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
import uuid
import os
import json
import time
import requests
import subprocess

# =========================
# CONFIG
# =========================

RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY")

RUNPOD_WHISPER_ENDPOINT = "30dqc4pwnknw0c"      # whisper worker
RUNPOD_TRANSLATOR_ENDPOINT = "30fje7gn3s3dt8"   # translator

RUNPOD_BASE = "https://api.runpod.ai/v2"
HEADERS = {
    "Authorization": f"Bearer {RUNPOD_API_KEY}",
    "Content-Type": "application/json"
}

BASE_DIR = "storage"
INPUT_DIR = f"{BASE_DIR}/input"
OUTPUT_DIR = f"{BASE_DIR}/output"
PROGRESS_DIR = f"{BASE_DIR}/progress"

os.makedirs(INPUT_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(PROGRESS_DIR, exist_ok=True)

app = FastAPI()

# =========================
# MODELS
# =========================

class UploadURL(BaseModel):
    video_url: str
    subtitle_preset: dict | None = None
    languages: list[str] | None = None  # ej ["eng_Latn","fra_Latn"]

# =========================
# RUNPOD HELPERS
# =========================

def run_runpod(endpoint_id: str, payload: dict) -> str:
    r = requests.post(
        f"{RUNPOD_BASE}/{endpoint_id}/run",
        headers=HEADERS,
        json={"input": payload},
        timeout=30
    )
    r.raise_for_status()
    return r.json()["id"]

def wait_runpod(endpoint_id: str, job_id: str) -> dict:
    while True:
        r = requests.get(
            f"{RUNPOD_BASE}/{endpoint_id}/status/{job_id}",
            headers=HEADERS,
            timeout=30
        )
        r.raise_for_status()
        data = r.json()

        if data["status"] == "COMPLETED":
            return data["output"]

        if data["status"] in ("FAILED", "CANCELLED"):
            raise RuntimeError(data)

        time.sleep(2)

# =========================
# PROGRESS
# =========================

def set_progress(job_id: str, percent: int):
    with open(f"{PROGRESS_DIR}/{job_id}.json", "w") as f:
        json.dump({"percent": percent}, f)

# =========================
# ASS GENERATOR (EXISTENTE)
# =========================

def generate_ass(segments, preset, out_path):
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("[Script Info]\n")
        f.write("ScriptType: v4.00+\n\n")
        f.write("[V4+ Styles]\n")
        f.write("Format: Name,Fontname,Fontsize,PrimaryColour,OutlineColour,BorderStyle,Outline,Alignment,MarginL,MarginR,MarginV\n")
        f.write(
            f"Style: Default,{preset['fontFamily']},{preset['fontSize']},&H00FFFFFF,&H00000000,1,{preset['outlineThickness']},2,30,30,30\n\n"
        )
        f.write("[Events]\n")
        f.write("Format: Layer,Start,End,Style,Text\n")

        for s in segments:
            start = s["start"]
            end = s["end"]
            text = s["text"].replace("\n", " ")
            f.write(f"Dialogue: 0,0:{start:.2f},0:{end:.2f},Default,{text}\n")

# =========================
# PIPELINE
# =========================

def process_video(job_id: str, data: UploadURL):
    try:
        set_progress(job_id, 5)

        # ---------- WHISPER ----------
        whisper_job = run_runpod(
            RUNPOD_WHISPER_ENDPOINT,
            {"video_url": data.video_url}
        )

        whisper_output = wait_runpod(
            RUNPOD_WHISPER_ENDPOINT,
            whisper_job
        )

        segments = whisper_output["segments"]
        source_lang = whisper_output["language"]

        set_progress(job_id, 40)

        # ---------- TRANSLATIONS (OPTIONAL) ----------
        translations = {}

        if data.languages:
            for lang in data.languages:
                tr_job = run_runpod(
                    RUNPOD_TRANSLATOR_ENDPOINT,
                    {
                        "source_language": source_lang,
                        "target_language": lang,
                        "segments": segments
                    }
                )
                tr_out = wait_runpod(RUNPOD_TRANSLATOR_ENDPOINT, tr_job)
                translations[lang] = tr_out["segments"]

        set_progress(job_id, 60)

        # ---------- ASS + FFMPEG ----------
        final_outputs = {}

        all_versions = {"original": segments} | translations

        for key, segs in all_versions.items():
            ass_path = f"{OUTPUT_DIR}/{job_id}_{key}.ass"
            mp4_path = f"{OUTPUT_DIR}/{job_id}_{key}.mp4"

            generate_ass(segs, data.subtitle_preset, ass_path)

            subprocess.run([
                "ffmpeg", "-y",
                "-i", f"{INPUT_DIR}/{job_id}.mp4",
                "-vf", f"ass={ass_path}",
                mp4_path
            ], check=True)

            final_outputs[key] = mp4_path

        set_progress(job_id, 100)

    except Exception as e:
        set_progress(job_id, -1)
        raise e

# =========================
# API
# =========================

@app.post("/upload")
def upload_url(data: UploadURL, background: BackgroundTasks):
    job_id = str(uuid.uuid4())
    open(f"{INPUT_DIR}/{job_id}.mp4", "wb").close()
    background.add_task(process_video, job_id, data)
    return {"job_id": job_id}

@app.get("/progress/{job_id}")
def progress(job_id: str):
    path = f"{PROGRESS_DIR}/{job_id}.json"
    if not os.path.exists(path):
        return {"percent": 0}
    with open(path) as f:
        return json.load(f)

@app.get("/download/{job_id}")
def download(job_id: str):
    files = [f for f in os.listdir(OUTPUT_DIR) if f.startswith(job_id)]
    if not files:
        raise HTTPException(404, "Not ready")
    return {
        "files": files
    }

@app.get("/")
def health():
    return {"ok": True}
