# =========================
# render-backend/main.py
# =========================

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
RUNPOD_WORKER_ENDPOINT_ID = os.getenv("RUNPOD_ENDPOINT_ID")
RUNPOD_TRANSLATOR_ENDPOINT_ID = os.getenv("RUNPOD_TRANSLATOR_ENDPOINT_ID")

if not RUNPOD_API_KEY or not RUNPOD_WORKER_ENDPOINT_ID:
    raise RuntimeError("RUNPOD_API_KEY or RUNPOD_ENDPOINT_ID missing")


# =========================
# PATHS
# =========================

BASE = os.path.dirname(os.path.abspath(__file__))
STO = os.path.join(BASE, "storage")
PRO = os.path.join(STO, "progress")
URLS = os.path.join(STO, "urls")
PRESETS = os.path.join(STO, "presets")
META = os.path.join(STO, "meta")
SEGS = os.path.join(STO, "segments")
LANGDONE = os.path.join(STO, "lang_done")

for p in (PRO, URLS, PRESETS, META, SEGS, LANGDONE):
    os.makedirs(p, exist_ok=True)


# =========================
# APP
# =========================

app = FastAPI(title="ClipFile Backend", version="1.2")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://sgpwlh-my-site-teyd1jsn-othmanebenbrahim12.wix-vibe.com",
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
    subtitle_preset: dict
    languages: list[str] | None = None


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


def mark_language_done(job_id: str, lang: str):
    path = os.path.join(LANGDONE, f"{job_id}.json")
    done = []
    if os.path.exists(path):
        done = json.load(open(path))
    if lang not in done:
        done.append(lang)
    with open(path, "w") as f:
        json.dump(done, f)


def all_languages_done(job_id: str) -> bool:
    meta = json.load(open(os.path.join(META, f"{job_id}.json")))
    langs = meta.get("languages", [])
    path = os.path.join(LANGDONE, f"{job_id}.json")
    if not os.path.exists(path):
        return False
    done = json.load(open(path))
    return set(done) == set(langs)


# =========================
# ROUTES
# =========================

@app.post("/upload")
def upload_url(body: UploadURL):
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

    runpod_id = r.json()["id"]
    open(os.path.join(PRO, f"{job_id}.runpod"), "w").write(runpod_id)

    write_progress(job_id, 20)
    return {"job_id": job_id}


@app.get("/progress/{job_id}")
def progress(job_id: str):
    current = read_progress(job_id)
    if current in (100, -1):
        return {"percent": current}

    meta = os.path.join(PRO, f"{job_id}.runpod")
    if not os.path.exists(meta):
        return {"percent": current}

    runpod_id = open(meta).read().strip()

    r = requests.get(
        f"https://api.runpod.ai/v2/{RUNPOD_WORKER_ENDPOINT_ID}/status/{runpod_id}",
        headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"},
        timeout=10,
    )

    if r.status_code != 200:
        return {"percent": current}

    data = r.json()
    status = data.get("status")

    if status == "COMPLETED":
        output = data.get("output") or {}

        base_url = output.get("base_url")
        segments = output.get("segments")

        if not base_url or not segments:
            write_progress(job_id, -1)
            return {"percent": -1}

        open(os.path.join(URLS, f"{job_id}_orig.txt"), "w").write(base_url)
        json.dump(segments, open(os.path.join(SEGS, f"{job_id}_orig.json"), "w"))

        languages = json.load(open(os.path.join(META, f"{job_id}.json"))).get("languages", [])

        if languages and RUNPOD_TRANSLATOR_ENDPOINT_ID:
            for lang in languages:
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
            write_progress(job_id, 100)

    elif status == "FAILED":
        write_progress(job_id, -1)
    else:
        write_progress(job_id, max(current, 50))

    return {"percent": read_progress(job_id)}


@app.post("/translator-callback")
def translator_callback(data: dict):
    job_id = data["job_id"]
    language = data["language"]
    segments = data["segments"]

    seg_path = os.path.join(SEGS, f"{job_id}_{language}.json")
    json.dump(segments, open(seg_path, "w"))

    r = requests.post(
        f"https://api.runpod.ai/v2/{RUNPOD_WORKER_ENDPOINT_ID}/run",
        headers={
            "Authorization": f"Bearer {RUNPOD_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "input": {
                "job_id": job_id,
                "segments": segments,
                "language": language,
            }
        },
    )

    if r.status_code != 200:
        raise HTTPException(500, r.text)

    url = r.json()["output"]["base_url"]
    open(os.path.join(URLS, f"{job_id}_{language}.txt"), "w").write(url)

    mark_language_done(job_id, language)

    if all_languages_done(job_id):
        write_progress(job_id, 100)

    return {"ok": True}


@app.get("/download/{job_id}")
def download(job_id: str, lang: str | None = None):
    suffix = lang or "orig"
    path = os.path.join(URLS, f"{job_id}_{suffix}.txt")
    if not os.path.exists(path):
        raise HTTPException(404, "Not ready")
    return RedirectResponse(open(path).read().strip())
