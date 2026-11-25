import base64
import logging
import os
import subprocess
import threading
import time
from typing import Dict, Optional, Tuple

import yt_dlp
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("otto-music")

app = FastAPI(title="Otto Music Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

COOKIES_PATH = "/tmp/youtube_cookies.txt"
START_TIME = time.time()
EXTRACT_LOCK = threading.Lock()

# Cache: track_key -> (stream_url, http_headers, webpage_url, expires_at)
STREAM_CACHE: Dict[str, Tuple[str, dict, str, float]] = {}
STREAM_CACHE_TTL = 1800  # 30 phút


def prepare_cookies_file() -> Optional[str]:
    """Tạo file cookies YouTube từ biến môi trường YOUTUBE_COOKIES (nếu có)."""
    raw = os.environ.get("YOUTUBE_COOKIES", "").strip()
    if not raw:
        return None
    try:
        if "# Netscape HTTP Cookie File" not in raw and "\t" not in raw:
            decoded = base64.b64decode(raw).decode("utf-8", errors="ignore")
            if "\t" in decoded:
                raw = decoded
        with open(COOKIES_PATH, "w", encoding="utf-8") as f:
            f.write(raw)
        return COOKIES_PATH
    except Exception as e:
        logger.warning("Không thể đọc YOUTUBE_COOKIES: %s", e)
        return None


def pick_best_audio_url(entry: dict) -> Tuple[Optional[str], dict]:
    """Chọn URL audio tốt nhất (tránh bản preview 30s của SoundCloud Go+)."""
    headers = entry.get("http_headers") or {}
    formats = entry.get("formats") or []

    # Ưu tiên progressive mp3/m4a không phải preview
    full_formats = [
        f for f in formats
        if f.get("url")
        and "preview" not in str(f.get("format_id", "")).lower()
        and "preview" not in str(f.get("url", "")).lower()
    ]
    candidates = full_formats or [f for f in formats if f.get("url")]

    if candidates:
        # Ưu tiên http mp3 trước, rồi đến hls mp3, rồi bất kỳ audio nào
        def score(f: dict) -> tuple:
            proto = str(f.get("protocol", "")).lower()
            ext = str(f.get("ext", "")).lower()
            is_http = 1 if proto in ("http", "https") else 0
            is_mp3 = 1 if ext == "mp3" else 0
            abr = float(f.get("abr") or f.get("tbr") or 0)
            return (is_http, is_mp3, abr)

        best = max(candidates, key=score)
        return best["url"], best.get("http_headers") or headers

    return entry.get("url"), headers


def search_soundcloud(query: str) -> Optional[dict]:
    """Tìm nhạc trên SoundCloud (không cần cookies, không bị chặn IP cloud)."""
    opts = {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "source_address": "0.0.0.0",
        "format": "bestaudio/best",
    }
    try:
        logger.info("Searching SoundCloud: %s", query)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"scsearch5:{query}", download=False)
            entries = [e for e in (info.get("entries") or []) if e] if info else []
            if not entries:
                return None

            # Ưu tiên bài đầy đủ (duration >= 60s) và có link không phải preview
            valid = []
            for e in entries:
                stream_url, headers = pick_best_audio_url(e)
                if not stream_url:
                    continue
                dur = int(e.get("duration") or 0)
                is_preview = "preview" in stream_url.lower()
                valid.append((not is_preview, dur >= 60, dur, e, stream_url, headers))

            if not valid:
                return None

            # Chọn bài tốt nhất (ưu tiên không phải preview và dài >= 60s; giữ thứ tự relevance trong nhóm tốt nhất)
            best_group = [v for v in valid if v[0] and v[1]]
            chosen = best_group[0] if best_group else max(valid, key=lambda x: (x[0], x[1], x[2]))
            _, _, dur, entry, stream_url, headers = chosen

            track_id = f"sc_{entry.get('id', int(time.time()))}"
            webpage_url = entry.get("webpage_url") or ""
            STREAM_CACHE[track_id] = (stream_url, headers, webpage_url, time.time() + STREAM_CACHE_TTL)

            return {
                "track_id": track_id,
                "title": entry.get("title") or query,
                "artist": entry.get("uploader") or "SoundCloud",
                "duration": dur,
                "source": "soundcloud",
            }
    except Exception as e:
        logger.warning("SoundCloud search error for '%s': %s", query, e)
        return None


def search_youtube(query: str) -> Optional[dict]:
    """Dự phòng: tìm nhạc trên YouTube (dùng cookies nếu có)."""
    opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "source_address": "0.0.0.0",
        "remote_components": ["ejs:github"],
    }
    cookies = prepare_cookies_file()
    if cookies:
        opts["cookiefile"] = cookies

    try:
        logger.info("Searching YouTube fallback: %s", query)
        with EXTRACT_LOCK:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"ytsearch1:{query}", download=False)
                entries = [e for e in (info.get("entries") or []) if e] if info else []
                if not entries:
                    return None
                entry = entries[0]
                stream_url, headers = pick_best_audio_url(entry)
                if not stream_url:
                    return None

                vid = entry.get("id", "")
                if not vid:
                    return None
                webpage_url = entry.get("webpage_url") or f"https://www.youtube.com/watch?v={vid}"
                STREAM_CACHE[vid] = (stream_url, headers, webpage_url, time.time() + STREAM_CACHE_TTL)

                return {
                    "track_id": vid,
                    "title": entry.get("title") or query,
                    "artist": entry.get("uploader") or "YouTube",
                    "duration": int(entry.get("duration") or 0),
                    "source": "youtube",
                }
    except Exception as e:
        logger.warning("YouTube fallback error for '%s': %s", query, e)
        return None


def resolve_stream_for_play(track_id: str) -> Tuple[str, dict]:
    now = time.time()
    cached = STREAM_CACHE.get(track_id)
    if cached and cached[3] > now:
        return cached[0], cached[1]

    # Nếu cache hết hạn hoặc bị restart, lấy lại từ webpage_url hoặc YouTube ID
    webpage_url = cached[2] if cached and cached[2] else ""
    if not webpage_url:
        if track_id.startswith("sc_"):
            raise HTTPException(status_code=404, detail="Track cache expired, please search again")
        webpage_url = f"https://www.youtube.com/watch?v={track_id}"

    opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "source_address": "0.0.0.0",
    }
    if not track_id.startswith("sc_"):
        opts["remote_components"] = ["ejs:github"]
        cookies = prepare_cookies_file()
        if cookies:
            opts["cookiefile"] = cookies

    with EXTRACT_LOCK:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(webpage_url, download=False)
            stream_url, headers = pick_best_audio_url(info)
            if not stream_url:
                raise RuntimeError("No playable audio stream found")
            STREAM_CACHE[track_id] = (stream_url, headers, webpage_url, time.time() + STREAM_CACHE_TTL)
            return stream_url, headers


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "Otto Music Server (SoundCloud + YouTube)",
        "uptime": int(time.time() - START_TIME),
        "cached_tracks": len(STREAM_CACHE),
        "has_cookies": bool(os.environ.get("YOUTUBE_COOKIES", "").strip()),
        "ytdlp_version": yt_dlp.version.__version__,
    }


@app.get("/stream_pcm")
@app.get("//stream_pcm")
def stream_pcm(
    song: str = Query(..., description="Tên bài hát"),
    artist: str = Query("", description="Tên ca sĩ (tùy chọn)"),
):
    query = f"{song} {artist}".strip()
    if not query:
        raise HTTPException(status_code=400, detail="Missing 'song' parameter")

    # Ưu tiên SoundCloud (ổn định 100%, không cần cookies, không chặn IP cloud)
    result = search_soundcloud(query)

    # Nếu SoundCloud không có bài, thử dự phòng YouTube
    if not result:
        result = search_youtube(query)

    if not result:
        raise HTTPException(status_code=404, detail=f"Không tìm thấy bài hát '{query}'")

    return {
        "title": result["title"],
        "artist": result["artist"],
        "audio_url": f"/play/{result['track_id']}.mp3",
        "duration": result["duration"],
        "source": result["source"],
    }


@app.get("/play/{track_id}.mp3")
def play_mp3(track_id: str):
    try:
        stream_url, http_headers = resolve_stream_for_play(track_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to resolve stream for %s: %s", track_id, e)
        raise HTTPException(status_code=500, detail=f"Failed to fetch audio: {e}")

    ffmpeg_cmd = [
        "ffmpeg",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
    ]
    if http_headers:
        header_str = "".join(f"{k}: {v}\r\n" for k, v in http_headers.items())
        ffmpeg_cmd += ["-headers", header_str]

    ffmpeg_cmd += [
        "-i", stream_url,
        "-vn",
        "-f", "mp3",
        "-ab", "128k",
        "-ar", "44100",
        "-ac", "2",
        "-loglevel", "error",
        "pipe:1",
    ]

    def generate():
        proc = subprocess.Popen(
            ffmpeg_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=16384,
        )
        try:
            while True:
                chunk = proc.stdout.read(8192)
                if not chunk:
                    break
                yield chunk
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
            if proc.stderr:
                err = proc.stderr.read().decode("utf-8", errors="ignore").strip()
                if err:
                    logger.warning("ffmpeg stderr (%s): %s", track_id, err)
                proc.stderr.close()
            if proc.stdout:
                proc.stdout.close()

    return StreamingResponse(
        generate(),
        media_type="audio/mpeg",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
def index():
    return """<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Otto Music Server</title>
  <style>
    body { font-family: system-ui, sans-serif; background: #0f172a; color: #e2e8f0; max-width: 640px; margin: 40px auto; padding: 0 16px; }
    .card { background: #1e293b; padding: 20px; border-radius: 12px; }
    input { width: 100%; padding: 10px; margin: 8px 0; border-radius: 8px; border: 1px solid #334155; background: #0f172a; color: #fff; box-sizing: border-box; }
    button { padding: 10px 18px; border: none; border-radius: 8px; background: #3b82f6; color: #fff; font-weight: 600; cursor: pointer; }
    button:hover { background: #2563eb; }
    pre { background: #0f172a; padding: 12px; border-radius: 8px; overflow-x: auto; font-size: 13px; }
    audio { width: 100%; margin-top: 12px; }
  </style>
</head>
<body>
  <div class="card">
    <h2>🎵 Otto Music Server</h2>
    <p>Nguồn phát nhạc ổn định 24/7 (SoundCloud + YouTube fallback) cho <b>Otto Robot / Xiaozhi ESP32</b>:</p>
    <input id="song" placeholder="Tên bài hát (vd: Lạc Trôi)" value="Lạc Trôi" />
    <input id="artist" placeholder="Ca sĩ (tùy chọn, vd: Sơn Tùng)" value="Sơn Tùng" />
    <button onclick="testSong()">Tìm & Phát thử</button>
    <pre id="out">Sẵn sàng...</pre>
    <audio id="player" controls></audio>
  </div>
  <script>
    async function testSong() {
      const song = document.getElementById('song').value;
      const artist = document.getElementById('artist').value;
      const out = document.getElementById('out');
      const player = document.getElementById('player');
      out.textContent = 'Đang tìm bài hát...';
      try {
        const res = await fetch('/stream_pcm?song=' + encodeURIComponent(song) + '&artist=' + encodeURIComponent(artist));
        const data = await res.json();
        out.textContent = JSON.stringify(data, null, 2);
        if (data.audio_url) {
          player.src = data.audio_url;
          player.play();
        }
      } catch (e) {
        out.textContent = 'Lỗi: ' + e;
      }
    }
  </script>
</body>
</html>"""
