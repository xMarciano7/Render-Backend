# =========================
# render-backend/main.py
# =========================

import os
import uuid
import json
import requests
import boto3

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import RedirectResponse
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

if not RUNPOD_API_KEY or not RUNPOD_WORKER_ENDPOINT_ID or not RUNPOD_TRANSLATOR_ENDPOINT_ID:
    raise RuntimeError("RunPod env vars missing")

if not R2_BUCKET or not R2_ACCOUNT_ID or not R2_ACCESS_KEY or not R2_SECRET_KEY or not R2_PUBLIC_BASE:
    raise RuntimeError("R2 env vars missing")


# =========================
# STORAGE
# =========================

BASE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(BASE, "storage")

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

app = FastAPI(title="ClipFile Backend", version="2.5-cors-final")

# ✅ CORS CORRECTO (NO headers manuales)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https://.*\.(wixsite|wix|wix-vibe)\.com",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)



# =========================
# OPTIONS (PRE-FLIGHT)
# =========================

@app.options("/upload-url")
def options_upload_url():
    return Response(status_code=204)

@app.options("/upload")
def options_upload():
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


def set_flag(job_id: str, name: str):
    open(os.path.join(FLAGS, f"{job_id}_{name}.flag"), "w").write("1")


def has_flag(job_id: str, name: str) -> bool:
    return os.path.exists(os.path.join(FLAGS, f"{job_id}_{name}.flag"))


def mark_lang_done(job_id: str, lang: str):
    path = os.path.join(LANG_DONE, f"{job_id}.json")
    done = json.load(open(path)) if os.path.exists(path) else []
    if lang not in done:
        done.append(lang)
    json.dump(done, open(path, "w"))


def all_langs_done(job_id: str) -> bool:
    langs = json.load(open(os.path.join(META, f"{job_id}.json"))).get("languages", [])
    if not langs:
        return True
    path = os.path.join(LANG_DONE, f"{job_id}.json")
    if not os.path.exists(path):
        return False
    done = json.load(open(path))
    return set(done) == set(langs)


def r2_client():
    return boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto"
    )


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
        Params={
            "Bucket": R2_BUCKET,
            "Key": key,
            "ContentType": "video/mp4",
        },
        ExpiresIn=3600,
    )

    return {
        "job_id": job_id,
        "upload_url": upload_url,
        "video_url": f"{R2_PUBLIC_BASE}/{key}",
    }


@app.post("/upload")
def upload(body: UploadURL):
    job_id = (
        body.job_id
        or extract_job_id_from_video_url(body.video_url)
        or str(uuid.uuid4())
    )

    write_progress(job_id, 5)

    json.dump(
        {
            "original": body.subtitle_preset_original,
            "translated": body.subtitle_preset_translated,
        },
        open(os.path.join(PRESETS, f"{job_id}.json"), "w")
    )

    json.dump(
        {"languages": body.languages or []},
        open(os.path.join(META, f"{job_id}.json"), "w")
    )

    open(os.path.join(CLEAN_URLS, f"{job_id}.txt")).write(body.video_url)

    r = requests.post(
        f"https://api.runpod.ai/v2/{RUNPOD_WORKER_ENDPOINT_ID}/run",
        headers={
            "Authorization": f"Bearer {RUNPOD_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "input": {
                "job_id": job_id,
                "video_url": body.video_url,
                "subtitle_preset": body.subtitle_preset_original,
            }
        },
        timeout=20,
    )

    if r.status_code != 200:
        write_progress(job_id, -1)
        raise HTTPException(500, r.text)

    open(os.path.join(RUNPOD_IDS, f"{job_id}_orig.txt"), "w").write(r.json()["id"])
    write_progress(job_id, 20)

    return {"job_id": job_id}


@app.get("/progress/{job_id}")
def progress(job_id: str):
    percent = read_progress(job_id)
    if percent in (100, -1):
        return {"percent": percent}

    orig_id_path = os.path.join(RUNPOD_IDS, f"{job_id}_orig.txt")
    if not os.path.exists(orig_id_path):
        return {"percent": percent}

    runpod_id = open(orig_id_path).read().strip()

    r = requests.get(
        f"https://api.runpod.ai/v2/{RUNPOD_WORKER_ENDPOINT_ID}/status/{runpod_id}",
        headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"},
        timeout=10,
    )

    if r.status_code != 200:
        return {"percent": percent}

    data = r.json()
    if data.get("status") == "COMPLETED" and not has_flag(job_id, "translator_started"):
        out = data.get("output", {})
        base_url = out.get("base_url")
        words = out.get("words")

        if base_url and words:
            open(os.path.join(URLS, f"{job_id}_orig.txt"), "w").write(base_url)
            json.dump(words, open(os.path.join(WORDS, f"{job_id}_orig.json"), "w"))

            langs = json.load(open(os.path.join(META, f"{job_id}.json"))).get("languages", [])

            if not langs:
                write_progress(job_id, 100)
                return {"percent": 100}

            for lang in langs:
                requests.post(
                    f"https://api.runpod.ai/v2/{RUNPOD_TRANSLATOR_ENDPOINT_ID}/run",
                    headers={
                        "Authorization": f"Bearer {RUNPOD_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "input": {
                            "job_id": job_id,
                            "source_language": "spa_Latn",
                            "target_language": lang,
                            "words": words,
                            "callback": "https://render-backend-1-xa46.onrender.com/translator-callback",
                        }
                    },
                )

            set_flag(job_id, "translator_started")
            write_progress(job_id, 80)

    return {"percent": read_progress(job_id)}


@app.post("/translator-callback")
def translator_callback(body: TranslatorCallback):
    job_id = body.job_id
    lang = body.language
    blocks = body.blocks

    json.dump(blocks, open(os.path.join(BLOCKS, f"{job_id}_{lang}.json"), "w"))

    clean_video_url = open(os.path.join(CLEAN_URLS, f"{job_id}.txt")).read().strip()
    preset_translated = json.load(
        open(os.path.join(PRESETS, f"{job_id}.json"))
    )["translated"]

    r = requests.post(
        f"https://api.runpod.ai/v2/{RUNPOD_WORKER_ENDPOINT_ID}/run",
        headers={
            "Authorization": f"Bearer {RUNPOD_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "input": {
                "job_id": job_id,
                "language": lang,
                "blocks": blocks,
                "base_video_url": clean_video_url,
                "subtitle_preset": preset_translated,
            }
        },
    )

    if r.status_code != 200:
        raise HTTPException(500, r.text)

    open(os.path.join(RUNPOD_IDS, f"{job_id}_{lang}.txt"), "w").write(r.json()["id"])
    mark_lang_done(job_id, lang)

    if all_langs_done(job_id):
        write_progress(job_id, 100)

    return {"ok": True}


@app.get("/download/{job_id}")
def download(job_id: str, lang: str | None = None):
    suffix = lang or "orig"
    path = os.path.join(URLS, f"{job_id}_{suffix}.txt")
    if not os.path.exists(path):
        raise HTTPException(404, "Not ready")
    return RedirectResponse(open(path).read().strip())
