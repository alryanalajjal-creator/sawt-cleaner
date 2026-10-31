import os, uuid, shutil, subprocess, threading, time, socket, ipaddress
from pathlib import Path
from typing import Dict, Any
from urllib.parse import urlparse, urljoin
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse

BASE = Path(__file__).resolve().parent
WORK = BASE / "work"
WORK.mkdir(exist_ok=True)
MAX_UPLOAD_GB = float(os.getenv("MAX_UPLOAD_GB", "20"))
JOBS: Dict[str, Dict[str, Any]] = {}
LOCK = threading.Lock()

app = FastAPI(title="Sawt Cleaner", version="1.0.0")

def run(cmd):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stdout[-5000:] or "فشلت عملية المعالجة.")
    return p.stdout

def set_job(job_id, **values):
    with LOCK:
        JOBS[job_id].update(values)


def validate_public_url(url: str):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("استخدم رابط فيديو مباشر يبدأ بـ http:// أو https://")
    if parsed.username or parsed.password:
        raise ValueError("الروابط التي تحتوي على بيانات دخول غير مدعومة.")
    host = parsed.hostname
    try:
        addresses = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror:
        raise ValueError("تعذّر العثور على خادم الرابط.")
    for item in addresses:
        ip = ipaddress.ip_address(item[4][0])
        if not ip.is_global:
            raise ValueError("هذا الرابط غير مسموح به لأسباب أمنية.")
    return parsed


YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
                 "youtu.be", "www.youtube-nocookie.com", "youtube-nocookie.com"}

def is_youtube_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in YOUTUBE_HOSTS

def download_youtube_video(url: str, destination_dir: Path) -> Path:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or (parsed.hostname or "").lower() not in YOUTUBE_HOSTS:
        raise ValueError("أدخل رابط YouTube صالحاً.")
    output_template = str(destination_dir / "youtube_video.%(ext)s")
    cmd = [
        "yt-dlp", "--no-playlist", "--no-warnings",
        "--restrict-filenames", "--max-filesize", f"{MAX_UPLOAD_GB}G",
        "-f", "bv*+ba/b", "--merge-output-format", "mp4",
        "-o", output_template, url
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if result.returncode != 0:
        raise ValueError("تعذّر تنزيل فيديو YouTube. قد يكون خاصاً أو محجوباً أو غير متاح للتنزيل.\n" + result.stdout[-1800:])
    candidates = [p for p in destination_dir.glob("youtube_video.*") if p.is_file()]
    candidates = [p for p in candidates if p.suffix.lower() not in (".part", ".ytdl", ".json")]
    if not candidates:
        raise ValueError("لم يعثر التطبيق على ملف الفيديو بعد التنزيل.")
    chosen = max(candidates, key=lambda p: p.stat().st_size)
    if chosen.stat().st_size > MAX_UPLOAD_GB * 1024**3:
        chosen.unlink(missing_ok=True)
        raise ValueError(f"حجم الفيديو يتجاوز الحد الحالي ({MAX_UPLOAD_GB:g} GB).")
    return chosen

def download_video_url(url: str, destination: Path):
    max_bytes = int(MAX_UPLOAD_GB * 1024**3)
    current = url
    for _ in range(6):
        parsed = validate_public_url(current)
        req = Request(current, headers={"User-Agent": "SawtCleaner/1.0", "Accept": "video/*,application/octet-stream,*/*"})
        try:
            response = urlopen(req, timeout=45)
        except HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                current = urljoin(current, e.headers["Location"])
                continue
            raise ValueError(f"تعذّر تحميل الرابط (HTTP {e.code}).")
        except (URLError, TimeoutError) as e:
            raise ValueError(f"تعذّر الاتصال بالرابط: {e}")
        with response:
            final_url = response.geturl()
            if final_url != current:
                validate_public_url(final_url)
            content_type = response.headers.get("Content-Type", "").lower()
            if content_type and not (content_type.startswith("video/") or "octet-stream" in content_type):
                raise ValueError("يبدو أن الرابط لا يشير إلى ملف فيديو مباشر. استخدم رابط تنزيل مباشر للفيديو.")
            length = response.headers.get("Content-Length")
            if length and int(length) > max_bytes:
                raise ValueError(f"حجم الفيديو يتجاوز الحد الحالي ({MAX_UPLOAD_GB:g} GB).")
            total = 0
            with destination.open("wb") as out:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError(f"حجم الفيديو يتجاوز الحد الحالي ({MAX_UPLOAD_GB:g} GB).")
                    out.write(chunk)
            if total == 0:
                raise ValueError("الرابط لا يحتوي على بيانات فيديو.")
            return
    raise ValueError("الرابط يحتوي على تحويلات كثيرة أو غير صالحة.")

def process_job(job_id: str, source: Path, original_name: str):
    job_dir = WORK / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    audio = job_dir / "source.wav"
    separated_dir = job_dir / "separated"
    final = job_dir / "cleaned_video.mp4"
    try:
        set_job(job_id, status="processing", progress=8, message="استخراج الصوت من الفيديو…")
        # Decode to high-quality PCM for separation.
        run(["ffmpeg", "-y", "-i", str(source), "-vn", "-ac", "2", "-ar", "44100",
             "-c:a", "pcm_s16le", str(audio)])
        set_job(job_id, progress=20, message="فصل الكلام عن الموسيقى بالذكاء الاصطناعي…")
        # Demucs keeps the vocal stem; speech generally lands in this stem.
        run(["python", "-m", "demucs", "-n", "htdemucs", "--two-stems=vocals",
             "-o", str(separated_dir), str(audio)])
        vocal = separated_dir / "htdemucs" / "source" / "vocals.wav"
        if not vocal.exists():
            # Demucs may use the input filename as the stem folder.
            candidates = list(separated_dir.glob("**/vocals.wav"))
            if not candidates:
                raise RuntimeError("لم يتم العثور على ملف الصوت المفصول.")
            vocal = candidates[0]
        set_job(job_id, progress=82, message="إعادة تركيب الفيديو مع الصوت المنظّف…")
        # Copy video bitstream unchanged; encode only the new audio track.
        run(["ffmpeg", "-y", "-i", str(source), "-i", str(vocal),
             "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
             "-c:a", "aac", "-b:a", "320k", "-shortest",
             "-movflags", "+faststart", str(final)])
        set_job(job_id, status="done", progress=100, message="اكتملت المعالجة.",
                output=str(final), download_name="sawt-cleaned.mp4")
    except Exception as e:
        set_job(job_id, status="error", progress=0, message="تعذّرت المعالجة.",
                error=str(e)[-2500:])
    finally:
        try:
            source.unlink(missing_ok=True)
            audio.unlink(missing_ok=True)
            shutil.rmtree(separated_dir, ignore_errors=True)
        except Exception:
            pass

@app.get("/")
def api_home():
    return {"name": "SAWT API", "status": "online", "docs": "/docs", "openapi": "/openapi.json"}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/api/jobs")
async def create_job(file: UploadFile = File(...)):
    name = (file.filename or "video").lower()
    if not name.endswith((".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi")):
        raise HTTPException(400, "ارفع ملف فيديو بصيغة MP4 أو MOV أو MKV أو WEBM أو AVI.")
    job_id = uuid.uuid4().hex
    job_dir = WORK / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    source = job_dir / "upload"  # Never trust the uploaded filename as a path.
    size = 0
    try:
        with source.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_GB * 1024**3:
                    raise HTTPException(413, f"حجم الملف يتجاوز الحد الحالي ({MAX_UPLOAD_GB:g} GB).")
                out.write(chunk)
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    finally:
        await file.close()
    with LOCK:
        JOBS[job_id] = {"status": "queued", "progress": 2, "message": "تم استلام الفيديو، بانتظار بدء المعالجة."}
    threading.Thread(target=process_job, args=(job_id, source, file.filename or "video"), daemon=True).start()
    return {"job_id": job_id}


@app.post("/api/jobs/url")
def create_job_from_url(payload: dict):
    url = str(payload.get("url", "")).strip()
    if not url:
        raise HTTPException(400, "أدخل رابط الفيديو.")
    job_id = uuid.uuid4().hex
    job_dir = WORK / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    source = job_dir / "upload"
    with LOCK:
        JOBS[job_id] = {"status": "queued", "progress": 1, "message": "تم استلام الرابط، جارٍ الاتصال بمصدر الفيديو."}

    def download_then_process():
        try:
            set_job(job_id, status="downloading", progress=4, message="جارٍ تنزيل الفيديو من الرابط…")
            if is_youtube_url(url):
                downloaded = download_youtube_video(url, job_dir)
                if downloaded != source:
                    shutil.move(str(downloaded), str(source))
            else:
                download_video_url(url, source)
            set_job(job_id, status="queued", progress=20, message="اكتمل تنزيل الفيديو، جارٍ بدء فصل الصوت…")
            process_job(job_id, source, "linked-video")
        except Exception as e:
            set_job(job_id, status="error", progress=0, message="تعذّر تنزيل الفيديو من الرابط.", error=str(e)[-2500:])
            shutil.rmtree(job_dir, ignore_errors=True)

    threading.Thread(target=download_then_process, daemon=True).start()
    return {"job_id": job_id}

@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "المهمة غير موجودة.")
        return {k: v for k, v in job.items() if k != "output"}

@app.get("/api/jobs/{job_id}/download")
def download(job_id: str):
    with LOCK:
        job = JOBS.get(job_id)
    if not job or job.get("status") != "done":
        raise HTTPException(404, "الملف غير جاهز للتنزيل.")
    path = Path(job["output"])
    if not path.exists():
        raise HTTPException(404, "انتهت صلاحية الملف أو لم يعد موجوداً.")
    return FileResponse(path, media_type="video/mp4", filename=job.get("download_name", "cleaned-video.mp4"))
