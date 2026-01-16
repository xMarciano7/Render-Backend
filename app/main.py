import os
import uuid
import json
import subprocess
import requests
import tempfile

import boto3
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel


# ================= ENV =================
RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY")
RUNPOD_WORKER_ENDPOINT_ID = os.getenv("RUNPOD_ENDPOINT_ID")
RUNPOD_TRANSLATOR_ENDPOINT_ID = os.getenv("RUNPOD_TRANSLATOR_ENDPOINT_ID")

R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID")
R2_ACCESS_KEY = os.getenv("R2_ACCESS_KEY")
R2_SECRET_KEY = os.getenv("R2_SECRET_KEY")
R2_BUCKET = os.getenv("R2_BUCKET")
R2_PUBLIC_BASE = os.getenv("R2_PUBLIC_BASE")

if not all([
    RUNPOD_API_KEY,
    RUNPOD_WORKER_ENDPOINT_ID,
    RUNPOD_TRANSLATOR_ENDPOINT_ID,
    R2_ACCOUNT_ID,
    R2_ACCESS_KEY,
    R2_SECRET_KEY,
    R2_BUCKET,
    R2_PUBLIC_BASE
]):
    raise RuntimeError("Missing env vars")


# ================= STORAGE =================
BASE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(BASE, "storage")

PROGRESS = os.path.join(STORE, "progress")
URLS = os.path.join(STORE, "urls")
META = os.path.join(STORE, "meta")
PRESETS = os.path.join(STORE, "presets")
SEGMENTS = os.path.join(STORE, "segments")
RUNPOD_IDS = os.path.join(STORE, "runpod_ids")
LANG_DONE = os.path.join(STORE, "lang_done")

for p in (PROGRESS, URLS, META, PRESETS, SEGMENTS, RUNPOD_IDS, LANG_DONE):
    os.makedirs(p, exist_ok=True)


# ================= APP =================
app = FastAPI(title="ClipFile Backend", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ================= MODELS =================
class UploadURL(BaseModel):
    youtube_url: str
    subtitle_preset: dict
    languages: list[str] | None = []


class TranslatorCallback(BaseModel):
    job_id: str
    language: str
    words: list[dict]


# ================= HELPERS =================
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


def run(cmd):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr)
    return p.stdout


def download_youtube(url: str, out_path: str):
    run([
        "yt-dlp",
        "-f", "bv*+ba/b",
        "--merge-output-format", "mp4",
        "-o", out_path,
        url
    ])
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError("YouTube download failed")


def upload_to_r2(local_path: str, key: str):
    s3 = boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto"
    )
    s3.upload_file(local_path, R2_BUCKET, key, ExtraArgs={"ContentType": "video/mp4"})
    return f"{R2_PUBLIC_BASE}/{key}"


# ================= ROUTES =================
@app.post("/upload")
def upload(body: UploadURL):
    job_id = str(uuid.uuid4())
    write_progress(job_id, 5)

    json.dump(body.subtitle_preset, open(os.path.join(PRESETS, f"{job_id}.json"), "w"))
    json.dump({"languages": body.languages or []}, open(os.path.join(META, f"{job_id}.json"), "w"))

    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "source.mp4")
        download_youtube(body.youtube_url, src)

        r2_key = f"inputs/{job_id}/source.mp4"
        video_url = upload_to_r2(src, r2_key)

    r = requests.post(
        f"https://api.runpod.ai/v2/{RUNPOD_WORKER_ENDPOINT_ID}/run",
        headers={
            "Authorization": f"Bearer {RUNPOD_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "input": {
                "job_id": job_id,
                "video_url": video_url,
                "subtitle_preset": body.subtitle_preset,
            }
        },
        timeout=30,
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

    pid = os.path.join(RUNPOD_IDS, f"{job_id}_orig.txt")
    if not os.path.exists(pid):
        return {"percent": percent}

    runpod_id = open(pid).read().strip()

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

        if base_url and segments:
            open(os.path.join(URLS, f"{job_id}_orig.txt"), "w").write(base_url)
            json.dump(segments, open(os.path.join(SEGMENTS, f"{job_id}_orig.json"), "w"))

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
                            "target_language": lang,
                            "segments": segments,
                            "callback": "https://render-backend.onrender.com/translator-callback",
                        }
                    },
                    timeout=30,
                )

            write_progress(job_id, 80)

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
        timeout=30,
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
