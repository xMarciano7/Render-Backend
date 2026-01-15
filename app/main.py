# =========================
# render-backend/main.py
# =========================

import os
import uuid
import json
import requests

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


# =========================
# ENV
# =========================

RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY")
RUNPOD_WORKER_ENDPOINT_ID = os.getenv("RUNPOD_ENDPOINT_ID")
RUNPOD_TRANSLATOR_ENDPOINT_ID = os.getenv("RUNPOD_TRANSLATOR_ENDPOINT_ID")

if not RUNPOD_API_KEY or not RUNPOD_WORKER_ENDPOINT_ID or not RUNPOD_TRANSLATOR_ENDPOINT_ID:
    raise RuntimeError("RunPod env vars missing")


# =========================
# STORAGE
# =========================

BASE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(BASE, "storage")

PROGRESS = os.path.join(STORE, "progress")
URLS = os.path.join(STORE, "urls")
PRESETS = os.path.join(STORE, "presets")
META = os.path.join(STORE, "meta")
SEGMENTS = os.path.join(STORE, "segments")
WORDS = os.path.join(STORE, "words")
RUNPOD_IDS = os.path.join(STORE, "runpod_ids")
LANG_DONE = os.path.join(STORE, "lang_done")

for p in (PROGRESS, URLS, PRESETS, META, SEGMENTS, WORDS, RUNPOD_IDS, LANG_DONE):
    os.makedirs(p, exist_ok=True)


# =========================
# APP
# =========================

app = FastAPI(title="ClipFile Backend", version="1.7")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================
# MODELS
# =========================

class UploadURL(BaseModel):
    youtube_url: str
    subtitle_preset: dict
    languages: list[str] | None = None


class TranslatorCallback(BaseModel):
    job_id: str
    language: str
    words: list[dict]


# =========================
# HELPERS
# =========================

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


def mark_lang_done(job_id: str, lang: str):
    path = os.path.join(LANG_DONE, f"{job_id}.json")
    done = json.load(open(path)) if os.path.exists(path) else []
    if lang not in done:
        done.append(lang)
    json.dump(done, open(path, "w"))


def all_langs_done(job_id: str) -> bool:
    meta = json.load(open(os.path.join(META, f"{job_id}.json")))
    langs = meta.get("languages", [])
    path = os.path.join(LANG_DONE, f"{job_id}.json")
    if not os.path.exists(path):
        return False
    done = json.load(open(path))
    return set(done) == set(langs)


# =========================
# ROUTES
# =========================

@app.post("/upload")
def upload(body: UploadURL):
    job_id = str(uuid.uuid4())
    write_progress(job_id, 5)

    json.dump(body.subtitle_preset, open(os.path.join(PRESETS, f"{job_id}.json"), "w"))
    json.dump({"languages": body.languages or []}, open(os.path.join(META, f"{job_id}.json"), "w"))

    r = requests.post(
        f"https://api.runpod.ai/v2/{RUNPOD_WORKER_ENDPOINT_ID}/run",
        headers={
            "Authorization": f"Bearer {RUNPOD_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "input": {
                "job_id": job_id,
                "youtube_url": body.youtube_url,
                "subtitle_preset": body.subtitle_preset,
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
    status = data.get("status")

    if status == "COMPLETED":
        out = data.get("output", {})
        base_url = out.get("base_url")
        segments = out.get("segments")
        words = out.get("words")

        if base_url and segments and words:
            open(os.path.join(URLS, f"{job_id}_orig.txt"), "w").write(base_url)
            json.dump(segments, open(os.path.join(SEGMENTS, f"{job_id}_orig.json"), "w"))
            json.dump(words, open(os.path.join(WORDS, f"{job_id}_orig.json"), "w"))

            langs = json.load(open(os.path.join(META, f"{job_id}.json"))).get("languages", [])

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
                            "segments": segments,
                            "callback": "https://render-backend-1-xa46.onrender.com/translator-callback",
                        }
                    },
                )

            write_progress(job_id, 80)
        else:
            write_progress(job_id, 60)

    elif status == "FAILED":
        write_progress(job_id, -1)
    else:
        write_progress(job_id, max(percent, 50))

    return {"percent": read_progress(job_id)}


@app.post("/translator-callback")
def translator_callback(body: TranslatorCallback):
    job_id = body.job_id
    lang = body.language
    words = body.words

    json.dump(words, open(os.path.join(WORDS, f"{job_id}_{lang}.json"), "w"))

    base_video_url = open(os.path.join(URLS, f"{job_id}_orig.txt")).read().strip()

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
                "words": words,
                "base_video_url": base_video_url,
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
