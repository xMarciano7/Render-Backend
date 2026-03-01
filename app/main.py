# =========================
# render-backend/main.py
# =========================

import os
import uuid
import json
import re
import io
import zipfile
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
import requests
import boto3
from glob import glob
from typing import Literal

from fastapi import FastAPI, HTTPException, Response, Query, Request
from fastapi.responses import StreamingResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# =========================
# GEMINI
# =========================

from google import genai

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-pro-preview")

if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY missing")

gemini_client = genai.Client(api_key=GEMINI_API_KEY)


def gemini_generate(prompt: str) -> str:
    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
    )
    return (response.text or "").strip()

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

if not RUNPOD_API_KEY or not RUNPOD_WORKER_ENDPOINT_ID:
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
FLAGS = os.path.join(STORE, "flags")
CLEAN_URLS = os.path.join(STORE, "clean_urls")
PLANS = os.path.join(STORE, "plans")
TRASH = os.path.join(STORE, "trash")

for p in (PROGRESS, URLS, PRESETS, META, WORDS, BLOCKS, RUNPOD_IDS, FLAGS, CLEAN_URLS, PLANS, TRASH):
    os.makedirs(p, exist_ok=True)


# =========================
# APP
# =========================

app = FastAPI(title="ClipFile Backend", version="4.8-wysiwyg-highlight")

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r".*",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.options("/{path:path}")
def options_all(path: str):
    return Response(status_code=204)


# =========================
# MODELS
# =========================

class ClipDefinition(BaseModel):
    startSec: float
    durationSec: float


class ClipDistribution(BaseModel):
    mode: Literal["full_per_language", "distributed"] = "full_per_language"
    perLanguage: dict[str, int] | None = None


class UploadURL(BaseModel):
    job_id: str | None = None
    video_url: str | None = None
    video_url_top: str | None = None
    video_url_bottom: str | None = None
    subtitle_preset_original: dict
    subtitle_preset_translated: dict
    languages: list[str] | None = None
    overlay_hook_original: dict | None = None
    overlay_hook_translated: dict | None = None
    enable_subtitles: bool | None = True
    enable_overlay: bool | None = False
    clipCount: int
    exportQuality: Literal["1080p", "2k", "4k"]
    generationMode: Literal["chronological_fixed_duration", "ai_moments_fixed_duration", "ai_highlights_fixed_duration", "ai_full"]
    clipDurationSec: int | None = None
    clips: list[ClipDefinition] | None = None
    clipDistribution: ClipDistribution | None = None


class WorkerCallback(BaseModel):
    job_id: str
    base_url: str | None = None
    words: list | None = None
    segments: list[dict] | None = None
    transcript: str | None = None
    durationTotal: float | None = None


class TranslatorCallback(BaseModel):
    job_id: str
    language: str
    blocks: list


class ClipRef(BaseModel):
    job_id: str
    target: str | None = None
    clip_id: str | None = None
    label: str | None = None
    type: Literal["original", "translated"] | None = None
    language: str | None = None
    languageLabel: str | None = None
    original_index: int | None = None
    order_key: float | None = None
    created_at: float | None = None


class ClipRefsPayload(BaseModel):
    clips: list[ClipRef]
    zip_name: str | None = None


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
        return 0


def _meta_path(job_id: str) -> str:
    return os.path.join(META, f"{job_id}.json")


def _read_meta(job_id: str) -> dict:
    path = _meta_path(job_id)
    if not os.path.exists(path):
        return {}
    try:
        return json.load(open(path))
    except Exception:
        return {}


def _write_meta(job_id: str, meta: dict):
    json.dump(meta, open(_meta_path(job_id), "w"))


def set_job_error(job_id: str, message: str):
    meta = _read_meta(job_id)
    if not meta:
        return
    meta["state"] = "error"
    meta["error_message"] = str(message or "Unknown processing error")[:1000]
    _write_meta(job_id, meta)


def is_job_cancel_requested(job_id: str) -> bool:
    meta = _read_meta(job_id)
    return bool(meta.get("state") == "cancel_requested")




def cancel_runpod_job(runpod_id: str):
    endpoints = [
        f"https://api.runpod.ai/v2/{RUNPOD_WORKER_ENDPOINT_ID}/cancel/{runpod_id}",
        f"https://api.runpod.ai/v2/{RUNPOD_WORKER_ENDPOINT_ID}/terminate/{runpod_id}",
        f"https://api.runpod.ai/v2/{RUNPOD_WORKER_ENDPOINT_ID}/stop/{runpod_id}",
    ]
    for endpoint in endpoints:
        try:
            requests.post(
                endpoint,
                headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"},
                timeout=15,
            )
        except Exception:
            continue


def cancel_runpod_jobs_for_app_job(job_id: str):
    runpod_files = glob(os.path.join(RUNPOD_IDS, f"{job_id}_*.txt"))
    for fp in runpod_files:
        try:
            runpod_id = open(fp).read().strip()
        except Exception:
            continue
        if runpod_id:
            cancel_runpod_job(runpod_id)


def check_runpod_failure(job_id: str) -> tuple[bool, str | None]:
    runpod_files = glob(os.path.join(RUNPOD_IDS, f"{job_id}_*.txt"))
    for fp in runpod_files:
        try:
            runpod_id = open(fp).read().strip()
        except Exception:
            continue
        if not runpod_id:
            continue
        try:
            r = requests.get(
                f"https://api.runpod.ai/v2/{RUNPOD_WORKER_ENDPOINT_ID}/status/{runpod_id}",
                headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"},
                timeout=8,
            )
            if r.status_code != 200:
                continue
            payload = r.json()
            status = str(payload.get("status", "")).upper()
            if status in ("FAILED", "TIMED_OUT", "CANCELLED"):
                err = payload.get("error") or payload.get("output") or payload.get("status")
                return True, str(err)[:1000]
        except Exception:
            continue
    return False, None


def r2_client():
    return boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto",
    )


def all_translated_ready(job_id: str) -> bool:
    langs = json.load(open(os.path.join(META, f"{job_id}.json"))).get("languages", [])
    for lang in langs:
        if not os.path.exists(os.path.join(URLS, f"{job_id}_{lang}.txt")):
            return False
    return True


def get_user_plan(request: Request) -> Literal["free", "pro", "ultra"]:
    # Backend-authoritative source should come from auth context.
    # In this service we map normalized plan headers injected by the upstream auth layer.
    raw = (
        request.headers.get("x-user-plan")
        or request.headers.get("x-plan")
        or "free"
    ).strip().lower()
    if raw in ("free", "pro", "ultra"):
        return raw
    return "free"


def get_resolution_for_quality(quality: str) -> dict:
    if quality == "2k":
        return {"width": 1440, "height": 2560}
    if quality == "4k":
        return {"width": 2160, "height": 3840}
    return {"width": 1080, "height": 1920}


def count_ready_original_clips(job_id: str) -> int:
    return len(glob(os.path.join(URLS, f"{job_id}_orig_*.txt")))


def read_json_if_exists(path: str):
    if not os.path.exists(path):
        return None
    try:
        return json.load(open(path))
    except Exception:
        return None


def _clip_valid(clip: dict, mode: str, clip_duration_sec: int | None, duration_total: float) -> bool:
    try:
        s = float(clip.get("startSec"))
        d = float(clip.get("durationSec"))
    except Exception:
        return False
    if s < 0:
        return False
    if mode in ("ai_moments_fixed_duration", "ai_highlights_fixed_duration"):
        if clip_duration_sec is None or abs(d - float(clip_duration_sec)) > 1e-6:
            return False
    else:
        if d < 5 or d > 120:
            return False
    if s + d > duration_total:
        return False
    return True


def fallback_chronological_clips(clip_count: int, duration_total: float, clip_duration_sec: int | None, mode: str) -> list[dict]:
    duration = float(clip_duration_sec if mode in ("ai_moments_fixed_duration", "ai_highlights_fixed_duration") else 30.0)
    duration = max(5.0, min(120.0, duration))
    if duration_total <= 0:
        duration_total = duration * clip_count
    clips: list[dict] = []
    start = 0.0
    for _ in range(clip_count):
        if start + duration > duration_total:
            start = max(0.0, duration_total - duration)
        clips.append({"startSec": round(start, 3), "durationSec": round(duration, 3)})
        start += duration
    return clips


def parse_gemini_clips(raw_text: str) -> list[dict]:
    text = (raw_text or "").strip()
    if not text:
        return []
    if "```" in text:
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
        if m:
            text = m.group(1)
    try:
        obj = json.loads(text)
    except Exception:
        return []
    clips = obj.get("clips") if isinstance(obj, dict) else None
    return clips if isinstance(clips, list) else []


def gemini_pick_clips(segments: list[dict], clip_count: int, mode: str, clip_duration_sec: int | None, duration_total: float) -> list[dict]:
    constraint = (
        f"durationSec must be exactly {clip_duration_sec}."
        if mode in ("ai_moments_fixed_duration", "ai_highlights_fixed_duration")
        else "durationSec must be between 5 and 120."
    )
    prompt = (
        "Return ONLY valid JSON with exact schema: {\"clips\":[{\"startSec\":12.3,\"durationSec\":30.0}]}. "
        f"Need exactly {clip_count} clips. startSec >= 0. {constraint} "
        f"Always satisfy startSec+durationSec <= {duration_total}. "
        "Avoid overlap if possible. Use these timestamped transcript segments:\n"
        + json.dumps(segments[:600], ensure_ascii=False)
    )

    try:
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
        )
        return parse_gemini_clips(response.text or "")
    except Exception:
        return []


def build_ai_final_clips(meta: dict) -> list[dict]:
    clip_count = int(meta.get("clipCount", 1))
    mode = str(meta.get("generationMode", "chronological_fixed_duration"))
    clip_duration = meta.get("clipDurationSec")
    duration_total = float(meta.get("durationTotal") or 0.0)
    segments = meta.get("segments") if isinstance(meta.get("segments"), list) else []

    if not segments:
        return fallback_chronological_clips(clip_count, duration_total, clip_duration, mode)

    picks = gemini_pick_clips(segments, clip_count, mode, clip_duration, duration_total)
    valid: list[dict] = []
    for c in picks:
        if _clip_valid(c, mode, clip_duration, duration_total):
            valid.append({"startSec": float(c["startSec"]), "durationSec": float(c["durationSec"])})
        if len(valid) >= clip_count:
            break

    if len(valid) < clip_count:
        fallback = fallback_chronological_clips(clip_count, duration_total, clip_duration, mode)
        for c in fallback:
            if len(valid) >= clip_count:
                break
            if _clip_valid(c, mode, clip_duration, duration_total):
                valid.append(c)

    return valid[:clip_count]


def dispatch_clip_jobs(job_id: str, base_worker_input: dict, clips: list[dict]):
    for index, clip_def in enumerate(clips):
        runpod_flag = os.path.join(FLAGS, f"{job_id}_clip_dispatched_{index}.txt")
        if os.path.exists(runpod_flag):
            continue

        worker_input = {
            **base_worker_input,
            "callback": f"{BASE_URL}/worker-callback?index={index}",
            "start_sec": float(clip_def["startSec"]),
            "duration_sec": float(clip_def["durationSec"]),
            "analysis_only": False,
        }
        r = requests.post(
            f"https://api.runpod.ai/v2/{RUNPOD_WORKER_ENDPOINT_ID}/run",
            headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"},
            json={"input": worker_input},
        )
        if r.status_code != 200:
            raise HTTPException(500, r.text)
        open(os.path.join(RUNPOD_IDS, f"{job_id}_orig_{index}.txt"), "w").write(r.json()["id"])
        open(runpod_flag, "w").write("1")


def resolve_suffix(job_id: str, target: str | None) -> str:
    if not target:
        return "orig_0"

    if target.isdigit():
        return f"orig_{int(target)}"

    return target


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def trash_path(job_id: str, suffix: str) -> str:
    return os.path.join(TRASH, f"{job_id}_{suffix}.json")


def parse_r2_key_from_url(url: str) -> str | None:
    try:
        parsed = urlparse(url)
        if not parsed.path:
            return None
        return parsed.path.lstrip("/")
    except Exception:
        return None


def delete_remote_asset(url: str):
    key = parse_r2_key_from_url(url)
    if not key:
        return
    try:
        r2_client().delete_object(Bucket=R2_BUCKET, Key=key)
    except Exception:
        # Keep operation best-effort so local state remains consistent even if remote object was already gone.
        pass


def list_trash_items() -> list[dict]:
    items: list[dict] = []
    for path in glob(os.path.join(TRASH, "*.json")):
        try:
            payload = json.load(open(path))
            payload["_path"] = path
            items.append(payload)
        except Exception:
            continue
    return items


def cleanup_expired_trash() -> int:
    removed = 0
    expiry = now_utc() - timedelta(days=15)
    for item in list_trash_items():
        deleted_at = item.get("deletedAt")
        if not deleted_at:
            continue
        try:
            deleted_dt = datetime.fromisoformat(deleted_at.replace("Z", "+00:00"))
        except Exception:
            continue
        if deleted_dt <= expiry:
            url = item.get("url")
            if isinstance(url, str) and url:
                delete_remote_asset(url)
            try:
                os.remove(item["_path"])
                removed += 1
            except Exception:
                pass
    return removed


def delete_clip_and_move_to_trash(clip: ClipRef) -> dict | None:
    suffix = resolve_suffix(clip.job_id, clip.target)
    path = os.path.join(URLS, f"{clip.job_id}_{suffix}.txt")
    if not os.path.exists(path):
        return None

    url = open(path).read().strip()
    trash_item = {
        "clip_id": clip.clip_id,
        "job_id": clip.job_id,
        "target": clip.target,
        "suffix": suffix,
        "label": clip.label,
        "type": clip.type,
        "language": clip.language,
        "languageLabel": clip.languageLabel,
        "url": url,
        "deletedAt": now_utc().isoformat().replace("+00:00", "Z"),
        "original_index": clip.original_index,
        "order_key": clip.order_key,
        "created_at": clip.created_at,
    }

    json.dump(trash_item, open(trash_path(clip.job_id, suffix), "w"))
    os.remove(path)
    return trash_item


def validate_upload_constraints(body: UploadURL, plan: Literal["free", "pro", "ultra"]):
    if plan == "free":
        if body.clipCount != 1:
            raise HTTPException(400, "clipCount must be 1 on free plan")
    elif plan == "pro":
        if body.clipCount < 1 or body.clipCount > 5:
            raise HTTPException(400, "clipCount must be between 1 and 5 on pro plan")
    else:
        if body.clipCount < 1 or body.clipCount > 10:
            raise HTTPException(400, "clipCount must be between 1 and 10 on ultra plan")

    if plan == "free" and body.exportQuality != "1080p":
        raise HTTPException(400, "exportQuality must be 1080p on free plan")
    if plan == "pro" and body.exportQuality not in ("1080p", "2k"):
        raise HTTPException(400, "exportQuality must be 1080p or 2k on pro plan")

    if plan == "free" and body.generationMode != "chronological_fixed_duration":
        raise HTTPException(400, "generationMode must be chronological_fixed_duration on free plan")

    if body.generationMode == "ai_full":
        if body.clipDurationSec is not None or body.clips is not None:
            raise HTTPException(400, "For ai_full, clipDurationSec and clips must be null")
        return

    if body.generationMode in ("ai_moments_fixed_duration", "ai_highlights_fixed_duration"):
        if body.clips is not None:
            raise HTTPException(400, "For AI highlights, clips must be null")
        if body.clipDurationSec is None:
            raise HTTPException(400, "clipDurationSec is required for AI highlights")
        if body.clipDurationSec < 5 or body.clipDurationSec > 120:
            raise HTTPException(400, "clipDurationSec must be between 5 and 120")
        return

    if body.clips is not None:
        if plan != "ultra":
            raise HTTPException(400, "clips[] is available only on ultra plan")
        if body.clipDurationSec is not None:
            raise HTTPException(400, "clipDurationSec must be null when clips[] is provided")
        if len(body.clips) != body.clipCount:
            raise HTTPException(400, "len(clips) must match clipCount")
        for idx, clip in enumerate(body.clips):
            if clip.startSec < 0:
                raise HTTPException(400, f"clips[{idx}].startSec must be >= 0")
            if clip.durationSec < 5 or clip.durationSec > 120:
                raise HTTPException(400, f"clips[{idx}].durationSec must be between 5 and 120")
    else:
        if body.clipDurationSec is None:
            raise HTTPException(400, "clipDurationSec is required when clips[] is null")
        if body.clipDurationSec < 5 or body.clipDurationSec > 120:
            raise HTTPException(400, "clipDurationSec must be between 5 and 120")


def ensure_highlight_fields(preset: dict):
    preset.setdefault("enableWordHighlight", False)
    preset.setdefault("wordHighlightMode", None)
    preset.setdefault("wordHighlightColor", "#FFFF00")
    preset.setdefault("activeBoxOpacity", 1.0)
    preset.setdefault("enableEntranceAnimation", False)
    preset.setdefault("entranceAnimationType", "fade")
    preset.setdefault("entranceAnimationSpeed", 0.5)

    # 🔴 FIX CRÍTICO: UNIFICAR CLAVE PARA EL WORKER
    preset["subtitle_animation"] = preset.get("wordHighlightMode")

    return preset




def sanitize_overlay_text(value: str) -> str:
    text = (value or '')[:40]
    return '\n'.join(text.split('\n')[:4])


def build_overlay_hook(payload_overlay: dict | None, preset: dict) -> dict:
    payload_overlay = payload_overlay if isinstance(payload_overlay, dict) else {}

    # New contract from frontend: overlayText + overlayHookEnabled + overlayStyle
    raw_text = str(payload_overlay.get('overlayText') or payload_overlay.get('text') or preset.get('overlayText') or '')
    text = sanitize_overlay_text(raw_text)

    style = payload_overlay.get('overlayStyle')
    if not isinstance(style, dict):
        style = payload_overlay.get('style')
    if not isinstance(style, dict):
        style = preset.get('overlayStyle') if isinstance(preset.get('overlayStyle'), dict) else {}

    enabled_flag = bool(
        payload_overlay.get('overlayHookEnabled', payload_overlay.get('enabled', preset.get('overlayHookEnabled', False)))
    )
    enabled = bool(enabled_flag and text.strip())

    duration_mode = str(payload_overlay.get('overlayDurationMode') or payload_overlay.get('durationMode') or preset.get('overlayDurationMode') or 'full')
    if duration_mode not in {'quarter', 'half', 'three_quarters', 'full'}:
        duration_mode = 'full'

    return {
        'enabled': enabled,
        'text': text,
        'style': style or {},
        'durationMode': duration_mode,
    }

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
def upload(body: UploadURL, request: Request):
    job_id = body.job_id or str(uuid.uuid4())
    write_progress(job_id, 5)
    user_plan = get_user_plan(request)
    validate_upload_constraints(body, user_plan)

    languages = body.languages or []

    preset_original = ensure_highlight_fields(dict(body.subtitle_preset_original))
    preset_translated = ensure_highlight_fields(dict(body.subtitle_preset_translated))
    overlay_hook_original = build_overlay_hook(body.overlay_hook_original, preset_original)
    overlay_hook_translated = build_overlay_hook(body.overlay_hook_translated, preset_translated)

    wpb = max(1, min(int(preset_original.get("wordsPerBlock", 1)), 4))
    max_lines = 2 if int(preset_original.get("maxLines", 1)) == 2 else 1

    preset_original["wordsPerBlock"] = wpb
    preset_original["maxLines"] = max_lines
    preset_translated["wordsPerBlock"] = wpb
    preset_translated["maxLines"] = max_lines

    json.dump(
        {"original": preset_original, "translated": preset_translated},
        open(os.path.join(PRESETS, f"{job_id}.json"), "w"),
    )

    json.dump(
        {
            "plan": user_plan,
            "languages": languages,
            "wordsPerBlock": wpb,
            "maxLines": max_lines,
            "videoComposition": preset_original.get("videoComposition", "single"),
            "videoLayout": preset_original.get("videoLayout"),
            "enable_subtitles": bool(body.enable_subtitles),
            "enable_overlay": bool(body.enable_overlay),
            "clipCount": body.clipCount,
            "exportQuality": body.exportQuality,
            "generationMode": body.generationMode,
            "clipDurationSec": body.clipDurationSec,
            "clips": [c.model_dump() for c in body.clips] if body.clips is not None else None,
            "clipDistribution": body.clipDistribution.model_dump() if body.clipDistribution is not None else {"mode": "full_per_language", "perLanguage": None},
            "analysis_status": "analysis_pending" if body.generationMode in ("ai_moments_fixed_duration", "ai_highlights_fixed_duration", "ai_full") else "not_required",
            "state": "processing",
            "error_message": None,
        },
        open(os.path.join(META, f"{job_id}.json"), "w"),
    )

    composition = preset_original.get("videoComposition", "single")

    if composition == "split":
        if not body.video_url_top or not body.video_url_bottom:
            raise HTTPException(400, "Split mode requires video_url_top and video_url_bottom")

        json.dump(
            {"top": body.video_url_top, "bottom": body.video_url_bottom},
            open(os.path.join(CLEAN_URLS, f"{job_id}.json"), "w"),
        )

        base_worker_input = {
            "job_id": job_id,
            "video_url_top": body.video_url_top,
            "video_url_bottom": body.video_url_bottom,
            "subtitle_preset": preset_original,
            "overlay_hook": overlay_hook_original,
            "enable_subtitles": bool(body.enable_subtitles),
            "export_width": get_resolution_for_quality(body.exportQuality)["width"],
            "export_height": get_resolution_for_quality(body.exportQuality)["height"],
            "cancel_check_url": f"{BASE_URL}/cancel-status/{job_id}",
        }
    else:
        if not body.video_url:
            raise HTTPException(400, "Single mode requires video_url")

        open(os.path.join(CLEAN_URLS, f"{job_id}.txt"), "w").write(body.video_url)

        base_worker_input = {
            "job_id": job_id,
            "video_url": body.video_url,
            "subtitle_preset": preset_original,
            "overlay_hook": overlay_hook_original,
            "enable_subtitles": bool(body.enable_subtitles),
            "export_width": get_resolution_for_quality(body.exportQuality)["width"],
            "export_height": get_resolution_for_quality(body.exportQuality)["height"],
            "cancel_check_url": f"{BASE_URL}/cancel-status/{job_id}",
        }

    open(os.path.join(PLANS, f"{job_id}.json"), "w").write(json.dumps(base_worker_input))

    if languages and not RUNPOD_TRANSLATOR_ENDPOINT_ID:
        raise HTTPException(500, "RUNPOD_TRANSLATOR_ENDPOINT_ID missing")

    mode = body.generationMode
    if mode == "chronological_fixed_duration":
        clip_jobs = []
        if body.clips is not None:
            clip_jobs = [c.model_dump() for c in body.clips]
        else:
            duration = float(body.clipDurationSec or 30)
            clip_jobs = [
                {"startSec": i * duration, "durationSec": duration}
                for i in range(body.clipCount)
            ]
        dispatch_clip_jobs(job_id, base_worker_input, clip_jobs)
    else:
        # AI path: run mandatory analysis-only first, planning is done at callback.
        analysis_flag = os.path.join(FLAGS, f"{job_id}_analysis_dispatched.txt")
        if not os.path.exists(analysis_flag):
            analysis_input = {
                **base_worker_input,
                "callback": f"{BASE_URL}/worker-callback",
                "analysis_only": True,
            }
            r = requests.post(
                f"https://api.runpod.ai/v2/{RUNPOD_WORKER_ENDPOINT_ID}/run",
                headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"},
                json={"input": analysis_input},
            )
            if r.status_code != 200:
                raise HTTPException(500, r.text)
            open(os.path.join(RUNPOD_IDS, f"{job_id}_analysis.txt"), "w").write(r.json()["id"])
            open(analysis_flag, "w").write("1")
        write_progress(job_id, 10)

    write_progress(job_id, 20)

    return {"job_id": job_id, "clips": []}


# =========================
# WORKER CALLBACK
# =========================

@app.post("/worker-callback")
def worker_callback(body: WorkerCallback, lang: str | None = Query(default=None), index: int | None = Query(default=None)):
    job_id = body.job_id

    # Analysis-only callback path (idempotent)
    if body.segments is not None and body.transcript is not None and body.durationTotal is not None:
        meta_path = os.path.join(META, f"{job_id}.json")
        if not os.path.exists(meta_path):
            return {"status": "ignored"}
        meta = json.load(open(meta_path))

        if meta.get("analysis_status") == "planned" or meta.get("analysis_status") == "analysis_ready":
            return {"status": "ignored"}

        if meta.get("segments") and meta.get("transcript") and meta.get("durationTotal"):
            return {"status": "ignored"}

        meta["segments"] = body.segments
        meta["transcript"] = body.transcript
        meta["durationTotal"] = float(body.durationTotal)
        meta["analysis_status"] = "analysis_ready"
        json.dump(meta, open(meta_path, "w"))

        if meta.get("generationMode") in ("ai_moments_fixed_duration", "ai_highlights_fixed_duration", "ai_full"):
            planned_flag = os.path.join(FLAGS, f"{job_id}_planned_once.txt")
            if not os.path.exists(planned_flag):
                final_clips = build_ai_final_clips(meta)
                base_worker_input = read_json_if_exists(os.path.join(PLANS, f"{job_id}.json"))
                if not base_worker_input:
                    raise HTTPException(500, "missing planning input")
                dispatch_clip_jobs(job_id, base_worker_input, final_clips)
                meta["clips"] = final_clips
                meta["analysis_status"] = "planned"
                json.dump(meta, open(meta_path, "w"))
                open(planned_flag, "w").write("1")

        write_progress(job_id, 20)
        return {"status": "ok", "analysis": True}

    suffix = lang or f"orig_{index if index is not None else 0}"

    open(os.path.join(URLS, f"{job_id}_{suffix}.txt"), "w").write(body.base_url)

    if suffix.startswith("orig_") and body.words:
        orig_index = int(suffix.split("_")[-1])
        open(os.path.join(WORDS, f"{job_id}.json"), "w").write(json.dumps(body.words))

        meta = json.load(open(os.path.join(META, f"{job_id}.json")))
        langs = meta.get("languages", [])
        clip_count = int(meta.get("clipCount", 1))
        originals_ready = count_ready_original_clips(job_id)

        if not langs:
            if originals_ready >= clip_count:
                write_progress(job_id, 100)
                meta["state"] = "done"
                _write_meta(job_id, meta)
            else:
                write_progress(job_id, min(95, 20 + int((originals_ready / max(1, clip_count)) * 70)))
            return {"status": "ok"}

        write_progress(job_id, min(90, 20 + int((originals_ready / max(1, clip_count)) * 50)))

        if orig_index != 0:
            return {"status": "ok"}

        for l in langs:
            flag = os.path.join(FLAGS, f"{job_id}_translator_{l}.txt")
            if os.path.exists(flag):
                continue

            requests.post(
                f"https://api.runpod.ai/v2/{RUNPOD_TRANSLATOR_ENDPOINT_ID}/run",
                headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"},
                json={
                    "input": {
                        "job_id": job_id,
                        "source_language": "eng_Latn",
                        "target_language": l,
                        "words": body.words,
                        "wordsPerBlock": meta["wordsPerBlock"],
                        "callback": f"{BASE_URL}/translator-callback",
                    }
                },
            )

            open(flag, "w").write("1")

        write_progress(job_id, 80)
        return {"status": "ok"}

    if not suffix.startswith("orig_") and all_translated_ready(job_id):
        write_progress(job_id, 100)
        meta = _read_meta(job_id)
        if meta:
            meta["state"] = "done"
            _write_meta(job_id, meta)

    return {"status": "ok"}


# =========================
# TRANSLATOR CALLBACK
# =========================

@app.post("/translator-callback")
def translator_callback(body: TranslatorCallback):
    job_id = body.job_id
    lang = body.language
    if is_job_cancel_requested(job_id):
        return {"status": "cancelled"}

    open(os.path.join(BLOCKS, f"{job_id}_{lang}.json"), "w").write(json.dumps(body.blocks))

    preset = ensure_highlight_fields(
        json.load(open(os.path.join(PRESETS, f"{job_id}.json")))["translated"]
    )
    meta = json.load(open(os.path.join(META, f"{job_id}.json")))

    if meta.get("videoComposition") == "split":
        urls = json.load(open(os.path.join(CLEAN_URLS, f"{job_id}.json")))
        base_video_url = urls["top"]
    else:
        base_video_url = open(os.path.join(CLEAN_URLS, f"{job_id}.txt")).read().strip()

    r = requests.post(
        f"https://api.runpod.ai/v2/{RUNPOD_WORKER_ENDPOINT_ID}/run",
        headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"},
        json={
            "input": {
                "job_id": job_id,
                "blocks": body.blocks,
                "base_video_url": base_video_url,
                "subtitle_preset": preset,
                "callback": f"{BASE_URL}/worker-callback?lang={lang}",
                "overlay_hook": build_overlay_hook(None, preset),
                "enable_subtitles": bool(meta.get("enable_subtitles", True)),
            }
        },
    )

    if r.status_code == 200:
        open(os.path.join(RUNPOD_IDS, f"{job_id}_{lang}.txt"), "w").write(r.json()["id"])

    return {"status": "ok"}


# =========================
# PROGRESS
# =========================

@app.get("/progress/{job_id}")
def progress(job_id: str):
    percent = read_progress(job_id)
    meta = _read_meta(job_id)
    state = meta.get("state") or ("done" if percent >= 100 else "processing")
    error_message = meta.get("error_message")

    if state == "processing" and percent < 100:
        failed, err = check_runpod_failure(job_id)
        if failed:
            state = "error"
            error_message = err or "Worker failed while generating clips."
            set_job_error(job_id, error_message)

    return {"percent": percent, "state": state, "error_message": error_message}


@app.get("/cancel-status/{job_id}")
def cancel_status(job_id: str):
    return {"job_id": job_id, "cancel_requested": is_job_cancel_requested(job_id)}


@app.post("/cancel/{job_id}")
def cancel_generation(job_id: str):
    meta = _read_meta(job_id)
    if not meta:
        raise HTTPException(404, "job not found")
    meta["state"] = "cancel_requested"
    meta["error_message"] = "Generation canceled by user."
    _write_meta(job_id, meta)
    cancel_runpod_jobs_for_app_job(job_id)
    open(os.path.join(PROGRESS, f"{job_id}.txt"), "w").write("0")
    return {"status": "ok"}


# =========================
# PREVIEW / DOWNLOAD
# =========================

@app.get("/preview/{job_id}")
@app.get("/preview/{job_id}/{target}")
def preview(job_id: str, target: str | None = None):
    suffix = resolve_suffix(job_id, target)
    path = os.path.join(URLS, f"{job_id}_{suffix}.txt")
    if not os.path.exists(path):
        raise HTTPException(404)
    return RedirectResponse(open(path).read().strip(), 302)


def sanitize_download_filename(filename: str | None, fallback: str) -> str:
    raw = (filename or fallback or "clip").strip()
    safe = re.sub(r"[^a-zA-Z0-9._\- ()]", "_", raw)
    safe = safe.replace("..", ".")
    safe = safe.strip(" ._") or "clip"
    if not safe.lower().endswith(".mp4"):
        safe = f"{safe}.mp4"
    if len(safe) > 100:
        name, ext = os.path.splitext(safe)
        safe = f"{name[:95]}{ext or '.mp4'}"
    return safe


@app.get("/download/{job_id}")
@app.get("/download/{job_id}/{target}")
def download(job_id: str, target: str | None = None, filename: str | None = Query(default=None)):
    suffix = resolve_suffix(job_id, target)
    path = os.path.join(URLS, f"{job_id}_{suffix}.txt")
    if not os.path.exists(path):
        raise HTTPException(404)

    r = requests.get(open(path).read().strip(), stream=True)
    fallback_name = f"{job_id}_{suffix}.mp4"
    download_name = sanitize_download_filename(filename, fallback_name)

    return StreamingResponse(
        r.iter_content(1024 * 1024),
        media_type="video/mp4",
        headers={"Content-Disposition": f'attachment; filename="{download_name}"'},
    )


@app.post("/download-zip")
def download_zip(body: ClipRefsPayload):
    if not body.clips:
        raise HTTPException(400, "clips is required")

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for idx, clip in enumerate(body.clips):
            suffix = resolve_suffix(clip.job_id, clip.target)
            path = os.path.join(URLS, f"{clip.job_id}_{suffix}.txt")
            if not os.path.exists(path):
                continue

            source_url = open(path).read().strip()
            response = requests.get(source_url, stream=True)
            if response.status_code != 200:
                continue

            safe_label = re.sub(r"[^a-zA-Z0-9._-]", "_", clip.label or f"{clip.job_id}_{suffix}.mp4")
            if not safe_label.lower().endswith(".mp4"):
                safe_label = f"{safe_label}.mp4"
            folder = "original" if (clip.type or "") == "original" else (clip.languageLabel or clip.language or "translated")
            zip_path = f"{folder}/{idx+1:02d}_{safe_label}"
            zip_file.writestr(zip_path, response.content)

    zip_buffer.seek(0)
    zip_name = re.sub(r"[^a-zA-Z0-9._-]", "_", body.zip_name or "clips")
    if not zip_name.lower().endswith(".zip"):
        zip_name = f"{zip_name}.zip"

    return StreamingResponse(
        iter([zip_buffer.getvalue()]),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_name}"'},
    )


@app.post("/clips/delete")
def delete_clips_endpoint(body: ClipRefsPayload):
    cleanup_expired_trash()
    deleted = []
    missing = []
    for clip in body.clips:
        item = delete_clip_and_move_to_trash(clip)
        if item is None:
            missing.append({"job_id": clip.job_id, "target": clip.target})
        else:
            deleted.append(item)
    return {"deleted": deleted, "missing": missing}


@app.get("/trash")
def get_trash():
    cleanup_expired_trash()
    items = []
    for item in list_trash_items():
        items.append({k: v for k, v in item.items() if k != "_path"})
    items.sort(key=lambda x: x.get("deletedAt", ""), reverse=True)
    return {"items": items}


@app.post("/trash/restore")
def restore_trash_item(body: ClipRefsPayload):
    restored = []
    for clip in body.clips:
        suffix = resolve_suffix(clip.job_id, clip.target)
        t_path = trash_path(clip.job_id, suffix)
        if not os.path.exists(t_path):
            continue
        item = json.load(open(t_path))
        url = item.get("url")
        if not isinstance(url, str) or not url:
            continue
        open(os.path.join(URLS, f"{clip.job_id}_{suffix}.txt"), "w").write(url)
        os.remove(t_path)
        restored.append(item)
    return {"restored": restored}


@app.post("/trash/delete")
def delete_trash_items(body: ClipRefsPayload):
    removed = []
    missing = []
    for clip in body.clips:
        suffix = resolve_suffix(clip.job_id, clip.target)
        t_path = trash_path(clip.job_id, suffix)
        if not os.path.exists(t_path):
            missing.append({"job_id": clip.job_id, "target": clip.target})
            continue
        try:
            item = json.load(open(t_path))
        except Exception:
            item = {}
        url = item.get("url")
        if isinstance(url, str) and url:
            delete_remote_asset(url)
        try:
            os.remove(t_path)
            removed.append({"job_id": clip.job_id, "target": clip.target})
        except Exception:
            missing.append({"job_id": clip.job_id, "target": clip.target})
    return {"removed": removed, "missing": missing}


@app.post("/trash/empty")
def empty_trash():
    cleanup_expired_trash()
    removed = 0
    for item in list_trash_items():
        url = item.get("url")
        if isinstance(url, str) and url:
            delete_remote_asset(url)
        try:
            os.remove(item["_path"])
            removed += 1
        except Exception:
            pass
    return {"removed": removed}