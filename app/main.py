import os
import subprocess
import tempfile
import traceback
import uuid

import boto3
from runpod.serverless import start
from faster_whisper import WhisperModel


# ================= CONFIG =================
WHISPER_MODEL = "medium"
DEVICE = "cuda"
COMPUTE_TYPE = "float16"
MAX_DURATION = "75"

R2_BUCKET = os.getenv("R2_BUCKET")
R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID")
R2_ACCESS_KEY = os.getenv("R2_ACCESS_KEY")
R2_SECRET_KEY = os.getenv("R2_SECRET_KEY")
R2_PUBLIC_BASE = os.getenv("R2_PUBLIC_BASE")
# =========================================


def run(cmd):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"CMD FAILED:\n{' '.join(cmd)}\nSTDERR:\n{p.stderr}")
    return p.stdout


def ts(t):
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def ass_color(hex_color: str):
    hex_color = hex_color.lstrip("#")
    r = hex_color[0:2]
    g = hex_color[2:4]
    b = hex_color[4:6]
    return f"&H00{b}{g}{r}"


def ass_alignment(alignment: str):
    if alignment == "left":
        return 1
    if alignment == "right":
        return 3
    return 2


def map_preset_to_ass(preset: dict):
    font_size = int(preset["fontSize"] * 2)
    play_res_y = 1920
    margin_v = int((100 - preset["verticalPosition"]) / 100 * play_res_y)

    return {
        "font": preset["fontFamily"],
        "font_size": font_size,
        "bold": 1 if preset["fontWeight"] == "bold" else 0,
        "italic": 1 if preset["fontStyle"] == "italic" else 0,
        "text_color": ass_color(preset["textColor"]),
        "outline_color": ass_color(preset["outlineColor"]),
        "outline_size": int(preset["outlineThickness"]),
        "alignment": ass_alignment(preset["horizontalAlignment"]),
        "margin_v": margin_v,
    }


def generate_ass_from_words(words, ass_preset, path):
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{ass_preset['font']},{ass_preset['font_size']},{ass_preset['text_color']},{ass_preset['text_color']},{ass_preset['outline_color']},&H00000000,{ass_preset['bold']},{ass_preset['italic']},1,{ass_preset['outline_size']},0,{ass_preset['alignment']},60,60,{ass_preset['margin_v']},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    for w in words:
        lines.append(
            f"Dialogue: 0,{ts(w['start'])},{ts(w['end'])},Default,,0,0,0,,{w['word'].upper()}"
        )

    with open(path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(lines))


def generate_ass_from_segments(segments, ass_preset, path):
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{ass_preset['font']},{ass_preset['font_size']},{ass_preset['text_color']},{ass_preset['text_color']},{ass_preset['outline_color']},&H00000000,{ass_preset['bold']},{ass_preset['italic']},1,{ass_preset['outline_size']},0,{ass_preset['alignment']},60,60,{ass_preset['margin_v']},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    for s in segments:
        lines.append(
            f"Dialogue: 0,{ts(s['start'])},{ts(s['end'])},Default,,0,0,0,,{s['text'].upper()}"
        )

    with open(path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(lines))


def download_video(url, output_path):
    if "youtube.com" in url or "youtu.be" in url:
        run(["yt-dlp", "-f", "mp4", "-o", output_path, url])
    else:
        run(["curl", "-L", "--fail", url, "-o", output_path])


def handler(event):
    try:
        data = event["input"]

        preset = data["subtitle_preset"]
        ass_preset = map_preset_to_ass(preset)

        tmp = tempfile.mkdtemp()
        input_mp4 = os.path.join(tmp, "input.mp4")
        subs_ass = os.path.join(tmp, "subs.ass")
        output_mp4 = os.path.join(tmp, "output.mp4")

        # ========= MODO 1: JOB ORIGINAL =========
        if "youtube_url" in data:
            youtube_url = data["youtube_url"]

            audio_wav = os.path.join(tmp, "audio.wav")
            download_video(youtube_url, input_mp4)

            run([
                "ffmpeg", "-y",
                "-i", input_mp4,
                "-t", MAX_DURATION,
                "-ac", "1",
                "-ar", "16000",
                audio_wav
            ])

            model = WhisperModel(WHISPER_MODEL, device=DEVICE, compute_type=COMPUTE_TYPE)
            segments, _ = model.transcribe(audio_wav, word_timestamps=True)

            words = []
            segments_out = []

            for seg in segments:
                if seg.text:
                    segments_out.append({
                        "start": seg.start,
                        "end": seg.end,
                        "text": seg.text.strip()
                    })

                if seg.words:
                    for w in seg.words:
                        words.append({
                            "start": w.start,
                            "end": w.end,
                            "word": w.word
                        })

            if not words:
                raise RuntimeError("NO WORDS FROM WHISPER")

            generate_ass_from_words(words, ass_preset, subs_ass)

        # ========= MODO 2: JOB TRADUCIDO =========
        else:
            segments_out = data["segments"]
            base_video_url = data["base_video_url"]

            download_video(base_video_url, input_mp4)
            generate_ass_from_segments(segments_out, ass_preset, subs_ass)

        run([
            "ffmpeg", "-y",
            "-i", input_mp4,
            "-t", MAX_DURATION,
            "-filter_complex",
            (
                "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
                "crop=1080:1920,boxblur=20:1[bg];"
                "[0:v]scale=1080:-1:force_original_aspect_ratio=decrease[fg];"
                "[bg][fg]overlay=(W-w)/2:(H-h)/2,ass=" + subs_ass
            ),
            "-c:a", "copy",
            output_mp4
        ])

        s3 = boto3.client(
            "s3",
            endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=R2_ACCESS_KEY,
            aws_secret_access_key=R2_SECRET_KEY,
            region_name="auto"
        )

        key = f"clips/{uuid.uuid4()}.mp4"
        s3.upload_file(
            output_mp4,
            R2_BUCKET,
            key,
            ExtraArgs={"ContentType": "video/mp4"}
        )

        return {
            "status": "ok",
            "base_url": f"{R2_PUBLIC_BASE}/{key}",
            "segments": segments_out
        }

    except Exception:
        print(traceback.format_exc())
        raise


start({"handler": handler})
