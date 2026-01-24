# =========================
# render-backend/main.py
# =========================

import os
import uuid
import json
import requests
import boto3

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import StreamingResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


# =========================
# ENV
# =========================

RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY")
RUNPOD_WORKER_ENDPOINT_ID = os.getenv("RUNPOD_ENDPOINT_ID")
RUNPOD_TRANSLATOR_ENDPOINT_ID = os.getenv("RUNPOD_TRANSLATOR_ENDPOINT_ID")

R2_BUCKET = os.getenv("R2_BUCKET")
R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID")
R2_ACCESS_KEY = os.getenv("R2_ACCESS_KEY")
R2_SECRET_KEY = os.getenv("R2_SECRET_KEY")
R2_PUBLIC_BASE = os.getenv("R2_PUBLIC_BASE")

BASE_URL = os.getenv("BASE_URL")

if not RUNPOD_API_KEY or not RUNPOD_WORKER_ENDPOINT_ID or not RUNPOD_TRANSLATOR_ENDPOINT_ID:
    raise RuntimeError("RunPod env vars missing")

if not R2_BUCKET or not R2_ACCOUNT_ID or not R2_ACCESS_KEY or not R2_SECRET_KEY or not R2_PUBLIC_BASE:
    raise RuntimeError("R2 env vars missing")

if not BASE_URL:
    raise RuntimeError("BASE_URL missing")


# =========================
# STORAGE
# =========================

BASE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(BASE, "storage")
os.makedirs(STORE, exist_ok=True)

PROGRESS = os.path.join(STORE, "progress")
URLS = os.path.join(STORE, "urls")
PRESETS = os.path.join(STORE, "presets")
META = os.path.join(STORE, "meta")
WORDS = os.path.join(STORE, "words")
BLOCKS = os.path.join(STORE, "blocks")
RUNPOD_IDS = os.path.join(STORE, "runpod_ids")
LANG_DONE = os.path.join(STORE, "lang_done")
CLEAN_URLS = os.path.join(STORE, "clean_urls")
FLAGS = os.path.join(STORE, "flags")

for p in (
    PROGRESS, URLS, PRESETS, META,
    WORDS, BLOCKS, RUNPOD_IDS,
    LANG_DONE, CLEAN_URLS, FLAGS
):
    os.makedirs(p, exist_ok=True)


# =========================
# APP
# =========================

app = FastAPI(title="ClipFile Backend", version="3.1-preview-download-split")

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r".*",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================
# OPTIONS
# =========================

@app.options("/{path:path}")
def options_all(path: str):
    return Response(status_code=204)


# =========================
# MODELS
# =========================

class UploadURL(BaseModel):
    job_id: str | None = None
    video_url: str
    subtitle_preset_original: dict
    subtitle_preset_translated: dict
    languages: list[str] | None = None


class TranslatorCallback(BaseModel):
    job_id: str
    language: str
    blocks: list[dict]


# =========================
# HELPERS
# =========================

def extract_job_id_from_video_url(video_url: str) -> str | None:
    try:
        return video_url.split("/")[-1].replace(".mp4", "")
    except:
        return None


def write_progress(job_id: str, value: int):
    path = os.path.join(PROGRESS, f"{job_id}.txt")
    try:
        cur = int(open(path).read())
        if cur >= value:
            return
    except:
        pass
    open(path, "w").write(str(value))


def read_progress(job_id: str) -> int:
    try:
        return int(open(os.path.join(PROGRESS, f"{job_id}.txt")).read())
    except:
        return -1


def r2_client():
    return boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto"
    )


def all_clips_ready(job_id: str) -> bool:
    langs = json.load(open(os.path.join(META, f"{job_id}.json"))).get("languages", [])
    if not os.path.exists(os.path.join(URLS, f"{job_id}_orig.txt")):
        return False
    for lang in langs:
        if not os.path.exists(os.path.join(URLS, f"{job_id}_{lang}.txt")):
            return False
    return True


def check_worker_and_store(job_id: str, suffix: str):
    id_path = os.path.join(RUNPOD_IDS, f"{job_id}_{suffix}.txt")
    if not os.path.exists(id_path):
        return False

    runpod_id = open(id_path).read().strip()

    r = requests.get(
        f"https://api.runpod.ai/v2/{RUNPOD_WORKER_ENDPOINT_ID}/status/{runpod_id}",
        headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"},
        timeout=10,
    )

    if r.status_code != 200:
        return False

    data = r.json()
    if data.get("status") != "COMPLETED":
        return False

    out = data.get("output", {})
    base_url = out.get("base_url")
    if not base_url:
        return False

    # ===== FIX CLAVE: guardar words si existen =====
    words = out.get("words")
    if words:
        open(os.path.join(WORDS, f"{job_id}.json"), "w").write(json.dumps(words))

    open(os.path.join(URLS, f"{job_id}_{suffix}.txt"), "w").write(base_url)
    return True


# =========================
# ROUTES
# =========================

@app.post("/upload-url")
def upload_url():
    job_id = str(uuid.uuid4())
    key = f"uploads/{job_id}.mp4"

    s3 = r2_client()
    upload_url = s3.generate_presigned_url(
        ClientMethod="put_object",
        Params={"Bucket": R2_BUCKET, "Key": key, "ContentType": "video/mp4"},
        ExpiresIn=3600,
    )

    return {
        "job_id": job_id,
        "upload_url": upload_url,
        "video_url": f"{R2_PUBLIC_BASE}/{key}",
    }


@app.post("/upload")
def upload(body: UploadURL):
    job_id = body.job_id or extract_job_id_from_video_url(body.video_url) or str(uuid.uuid4())
    write_progress(job_id, 5)

    json.dump(
        {"original": body.subtitle_preset_original, "translated": body.subtitle_preset_translated},
        open(os.path.join(PRESETS, f"{job_id}.json"), "w")
    )

    json.dump(
        {"languages": body.languages or []},
        open(os.path.join(META, f"{job_id}.json"), "w")
    )

    open(os.path.join(CLEAN_URLS, f"{job_id}.txt"), "w").write(body.video_url)

    r = requests.post(
        f"https://api.runpod.ai/v2/{RUNPOD_WORKER_ENDPOINT_ID}/run",
        headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"},
        json={
            "input": {
                "job_id": job_id,
                "video_url": body.video_url,
                "subtitle_preset": body.subtitle_preset_original,
            }
        },
    )

    if r.status_code != 200:
        write_progress(job_id, -1)
        raise HTTPException(500, r.text)

    open(os.path.join(RUNPOD_IDS, f"{job_id}_orig.txt"), "w").write(r.json()["id"])
    write_progress(job_id, 20)

    return {"job_id": job_id}


# =========================
# TRANSLATOR CALLBACK
# =========================

@app.post("/translator-callback")
def translator_callback(body: TranslatorCallback):
    job_id = body.job_id
    lang = body.language

    open(os.path.join(BLOCKS, f"{job_id}_{lang}.json"), "w").write(
        json.dumps(body.blocks)
    )

    preset = json.load(open(os.path.join(PRESETS, f"{job_id}.json")))["translated"]
    base_video_url = open(os.path.join(URLS, f"{job_id}_orig.txt")).read().strip()

    r = requests.post(
        f"https://api.runpod.ai/v2/{RUNPOD_WORKER_ENDPOINT_ID}/run",
        headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"},
        json={
            "input": {
                "job_id": job_id,
                "blocks": body.blocks,
                "base_video_url": base_video_url,
                "subtitle_preset": preset,
            }
        },
    )

    if r.status_code == 200:
        open(os.path.join(RUNPOD_IDS, f"{job_id}_{lang}.txt"), "w").write(r.json()["id"])

    return {"status": "ok"}


@app.get("/progress/{job_id}")
def progress(job_id: str):
    if check_worker_and_store(job_id, "orig"):
        write_progress(job_id, 60)

        langs = json.load(open(os.path.join(META, f"{job_id}.json"))).get("languages", [])
        if not langs:
            return {"percent": read_progress(job_id)}

        words_path = os.path.join(WORDS, f"{job_id}.json")
        if not os.path.exists(words_path):
            return {"percent": read_progress(job_id)}

        words = json.load(open(words_path))

        for lang in langs:
            requests.post(
                f"https://api.runpod.ai/v2/{RUNPOD_TRANSLATOR_ENDPOINT_ID}/run",
                headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"},
                json={
                    "input": {
                        "job_id": job_id,
                        "source_language": "eng_Latn",
                        "target_language": lang,
                        "words": words,
                        "callback": f"{BASE_URL}/translator-callback",
                    }
                },
            )

        write_progress(job_id, 80)

    langs = json.load(open(os.path.join(META, f"{job_id}.json"))).get("languages", [])
    for lang in langs:
        check_worker_and_store(job_id, lang)

    if all_clips_ready(job_id):
        write_progress(job_id, 100)
        return {"percent": 100}

    return {"percent": read_progress(job_id)}


# =========================
# PREVIEW
# =========================

@app.get("/preview/{job_id}")
@app.get("/preview/{job_id}/{lang}")
@app.head("/preview/{job_id}")
@app.head("/preview/{job_id}/{lang}")
def preview(job_id: str, lang: str | None = None):
    suffix = lang or "orig"
    path = os.path.join(URLS, f"{job_id}_{suffix}.txt")
    if not os.path.exists(path):
        raise HTTPException(404, "Not ready")

    r2_url = open(path).read().strip()
    return RedirectResponse(url=r2_url, status_code=302)


# =========================
# DOWNLOAD
# =========================

@app.get("/download/{job_id}")
@app.get("/download/{job_id}/{lang}")
def download(job_id: str, lang: str | None = None):
    suffix = lang or "orig"
    path = os.path.join(URLS, f"{job_id}_{suffix}.txt")
    if not os.path.exists(path):
        raise HTTPException(404, "Not ready")

    r2_url = open(path).read().strip()
    r = requests.get(r2_url, stream=True)
    if r.status_code != 200:
        raise HTTPException(502, "Failed to fetch video")

    return StreamingResponse(
        r.iter_content(chunk_size=1024 * 1024),
        media_type="video/mp4",
        headers={"Content-Disposition": f'attachment; filename="{job_id}_{suffix}.mp4"'}
    )
