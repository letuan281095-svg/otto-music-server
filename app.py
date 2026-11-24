import base64
import logging
import os
import subprocess
import time
from typing import Optional

import yt_dlp
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("otto-music")

app = FastAPI(title="Otto YouTube Music Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

COOKIES_PATH = "/tmp/youtube_cookies.txt"
START_TIME = time.time()


def prepare_cookies_file() -> Optional[str]:
    """Tạo file cookies từ biến môi trường YOUTUBE_COOKIES (nếu có)."""
    raw = os.environ.get("YOUTUBE_COOKIES", "").strip()
    if not raw:
        return None
    try:
        # Hỗ trợ cả dạng text Netscape trực tiếp hoặc base64
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


def get_ydl_opts(extract_flat: bool = False) -> dict:
    opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "default_search": "ytsearch1",
        "source_address": "0.0.0.0",
        "remote_components": ["ejs:github"],
    }
    if extract_flat:
        opts["extract_flat"] = "in_playlist"
        opts.pop("format", None)
    cookies = prepare_cookies_file()
    if cookies:
        opts["cookiefile"] = cookies
    return opts


def search_youtube(song: str, artist: str = "") -> dict:
    query = f"{song} {artist}".strip()
    if not query:
        raise HTTPException(status_code=400, detail="Missing 'song' parameter")

    logger.info("Searching YouTube: %s", query)
    with yt_dlp.YoutubeDL(get_ydl_opts(extract_flat=True)) as ydl:
        info = ydl.extract_info(f"ytsearch1:{query}", download=False)
        if not info or "entries" not in info or not info["entries"]:
            raise HTTPException(status_code=404, detail="Song not found on YouTube")
        entry = info["entries"][0]
        if not entry:
            raise HTTPException(status_code=404, detail="Song not found on YouTube")
        return {
            "id": entry.get("id", ""),
            "title": entry.get("title") or song,
            "artist": entry.get("uploader") or artist or "YouTube",
            "duration": int(entry.get("duration") or 0),
        }


def get_audio_stream_url(video_id: str) -> str:
    url = f"https://www.youtube.com/watch?v={video_id}"
    with yt_dlp.YoutubeDL(get_ydl_opts()) as ydl:
        info = ydl.extract_info(url, download=False)
        stream_url = info.get("url")
        if not stream_url:
            raise RuntimeError("No audio stream URL found")
        return stream_url


@app.get("/health")
def health():
    deno_ok = os.path.exists("/usr/local/bin/deno")
    return {
        "status": "ok",
        "service": "Otto YouTube Music Server",
        "uptime": int(time.time() - START_TIME),
        "has_cookies": bool(os.environ.get("YOUTUBE_COOKIES", "").strip()),
        "has_deno": deno_ok,
        "ytdlp_version": yt_dlp.version.__version__,
    }


@app.get("/stream_pcm")
@app.get("//stream_pcm")
def stream_pcm(
    song: str = Query(..., description="Tên bài hát"),
    artist: str = Query("", description="Tên ca sĩ (tùy chọn)"),
):
    try:
        result = search_youtube(song, artist)
        video_id = result["id"]
        if not video_id:
            raise HTTPException(status_code=404, detail="Video ID not found")
        return {
            "title": result["title"],
            "artist": result["artist"],
            "audio_url": f"/play/{video_id}.mp3",
            "duration": result["duration"],
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Search error for '%s': %s", song, e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/play/{video_id}.mp3")
def play_mp3(video_id: str):
    try:
        stream_url = get_audio_stream_url(video_id)
    except Exception as e:
        logger.error("Failed to get stream URL for %s: %s", video_id, e)
        raise HTTPException(status_code=500, detail=f"Failed to fetch YouTube audio: {e}")

    ffmpeg_cmd = [
        "ffmpeg",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
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
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()

    return StreamingResponse(
        generate(),
        media_type="audio/mpeg",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@app.get("/", response_class=HTMLResponse)
def index():
    return """<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Otto YouTube Music Server</title>
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
    <h2>🎵 Otto YouTube Music Server</h2>
    <p>Test nhanh API <code>/stream_pcm</code> cho Xiaozhi / Otto Robot:</p>
    <input id="song" placeholder="Tên bài hát (vd: Lạc Trôi)" value="Lạc Trôi" />
    <input id="artist" placeholder="Ca sĩ (tùy chọn, vd: Sơn Tùng)" />
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
      out.textContent = 'Đang tìm trên YouTube...';
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
