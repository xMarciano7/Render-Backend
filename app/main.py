# app/main.py
import os, uuid, requests, base64
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from fastapi.responses import FileResponse

RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY")
RUNPOD_ENDPOINT_ID = os.getenv("RUNPOD_ENDPOINT_ID")
if not RUNPOD_API_KEY or not RUNPOD_ENDPOINT_ID:
    raise RuntimeError("Missing RUNPOD env vars")

BASE = os.path.dirname(os.path.abspath(__file__))
STO = os.path.join(BASE, "storage")
OUT = os.path.join(STO, "output")
PRO = os.path.join(STO, "progress")
os.makedirs(OUT, exist_ok=True)
os.makedirs(PRO, exist_ok=True)

app = FastAPI(docs_url="/docs", openapi_url="/openapi.json")

class UploadURL(BaseModel):
    video_url: str

def wp(job, v):
    open(os.path.join(PRO, f"{job}.txt"), "w").write(str(v))

def rp(job):
    try: return int(open(os.path.join(PRO, f"{job}.txt")).read())
    except: return -1

@app.get("/")
def root():
    return {"status":"ok"}

@app.post("/upload")
def upload_url(body: UploadURL):
    job_id = str(uuid.uuid4())
    wp(job_id, 5)

    r = requests.post(
        f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/run",
        headers={"Authorization": f"Bearer {RUNPOD_API_KEY}",
                 "Content-Type": "application/json"},
        json={"input":{"video_url": body.video_url}},
        timeout=20
    )
    if r.status_code != 200:
        wp(job_id, -1)
        raise HTTPException(500, r.text)

    open(os.path.join(PRO, f"{job_id}.runpod"), "w").write(r.json()["id"])
    wp(job_id, 20)
    return {"job_id": job_id}

@app.get("/progress/{job_id}")
def progress(job_id: str):
    m = os.path.join(PRO, f"{job_id}.runpod")
    if not os.path.exists(m):
        return {"percent": rp(job_id)}

    rid = open(m).read().strip()
    r = requests.get(
        f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/status/{rid}",
        headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"},
        timeout=10
    )
    if r.status_code != 200:
        return {"percent": rp(job_id)}

    st = r.json().get("status")
    if st == "COMPLETED":
        vb64 = r.json()["output"]["video_base64"]
        open(os.path.join(OUT, f"{job_id}.mp4"), "wb").write(base64.b64decode(vb64))
        wp(job_id, 100)
    elif st == "FAILED":
        wp(job_id, -1)
    else:
        wp(job_id, 50)

    return {"percent": rp(job_id)}

@app.get("/download/{job_id}")
def download(job_id: str):
    p = os.path.join(OUT, f"{job_id}.mp4")
    if not os.path.exists(p):
        raise HTTPException(404, "Not ready")
    return FileResponse(p, media_type="video/mp4")
