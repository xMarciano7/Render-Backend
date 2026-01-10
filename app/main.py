import os
import uuid
import requests

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from fastapi.responses import RedirectResponse

RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY")
RUNPOD_ENDPOINT_ID = os.getenv("RUNPOD_ENDPOINT_ID")

BASE = os.path.dirname(os.path.abspath(__file__))
STO = os.path.join(BASE, "storage")
PRO = os.path.join(STO, "progress")
URLS = os.path.join(STO, "urls")

os.makedirs(PRO, exist_ok=True)
os.makedirs(URLS, exist_ok=True)

app = FastAPI(docs_url="/docs", openapi_url="/openapi.json")


class UploadURL(BaseModel):
    video_url: str


def wp(job_id: str, value: int):
    """write progress"""
    path = os.path.join(PRO, f"{job_id}.txt")
    current = rp(job_id)

    # nunca retroceder progreso
    if current >= value:
        return

    with open(path, "w") as f:
        f.write(str(value))


def rp(job_id: str) -> int:
    """read progress"""
    try:
        return int(open(os.path.join(PRO, f"{job_id}.txt")).read())
    except:
        return -1


@app.post("/upload")
def upload_url(body: UploadURL):
    job_id = str(uuid.uuid4())
    wp(job_id, 5)

    r = requests.post(
        f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/run",
        headers={
            "Authorization": f"Bearer {RUNPOD_API_KEY}",
            "Content-Type": "application/json",
        },
        json={"input": {"video_url": body.video_url}},
        timeout=20,
    )

    if r.status_code != 200:
        wp(job_id, -1)
        raise HTTPException(500, r.text)

    runpod_id = r.json().get("id")
    if not runpod_id:
        wp(job_id, -1)
        raise HTTPException(500, "RunPod did not return job id")

    with open(os.path.join(PRO, f"{job_id}.runpod"), "w") as f:
        f.write(runpod_id)

    wp(job_id, 20)
    return {"job_id": job_id}


@app.get("/progress/{job_id}")
def progress(job_id: str):
    # si ya está terminado, no volvemos a tocar RunPod
    current = rp(job_id)
    if current == 100:
        return {"percent": 100}

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

    if status == "SUCCEEDED":
        output = data.get("output") or {}
        url = output.get("video_url")

        if not url:
            wp(job_id, -1)
            return {"percent": -1}

        with open(os.path.join(URLS, f"{job_id}.txt"), "w") as f:
            f.write(url)

        wp(job_id, 100)

    elif status == "FAILED":
        wp(job_id, -1)

    else:
        # en progreso real
        wp(job_id, 50)

    return {"percent": rp(job_id)}


@app.get("/download/{job_id}")
def download(job_id: str):
    path = os.path.join(URLS, f"{job_id}.txt")
    if not os.path.exists(path):
        raise HTTPException(404, "Not ready")

    return RedirectResponse(open(path).read().strip())
