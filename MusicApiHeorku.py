from fastapi import FastAPI, HTTPException, Header, Query, Depends, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import asyncio, os, time, shutil, re, glob, secrets, json, httpx, base64
from typing import Dict, Optional

# JioSaavn media decryption (pip install pycryptodome)
try:
    from Crypto.Cipher import DES
    from Crypto.Util.Padding import unpad
    _JIOSAAVN_OK = True
except Exception:
    _JIOSAAVN_OK = False

app = FastAPI()

# =========================================================
# CONFIG
# =========================================================

# Cross-platform paths: VPS keeps /home/ubuntu, Heroku/Docker uses /app.
_DEFAULT_BASE = "/home/ubuntu" if os.path.isdir("/home/ubuntu") and os.access("/home/ubuntu", os.W_OK) else os.path.dirname(os.path.abspath(__file__))
BASE_DIR     = os.environ.get("BASE_DIR", _DEFAULT_BASE)
CACHE_DIR    = os.path.join(BASE_DIR, "cache")
COOKIES_FILE = os.environ.get("COOKIES_FILE", os.path.join(BASE_DIR, "cookies.txt"))
os.makedirs(CACHE_DIR, exist_ok=True)

# External binaries
_DEFAULT_YTDLP = (
    "/home/ubuntu/myenv/bin/yt-dlp"
    if os.path.isfile("/home/ubuntu/myenv/bin/yt-dlp")
    else (shutil.which("yt-dlp") or "yt-dlp")
)

YTDLP_BIN = os.environ.get("YTDLP_BIN", _DEFAULT_YTDLP)

DENO_BIN = os.environ.get(
    "DENO_BIN",
    shutil.which("deno") or "/app/.deno/bin/deno"
)

NODE_BIN = os.environ.get(
    "NODE_BIN",
    shutil.which("node") or "/usr/bin/node"
)

YTDLP_COOKIES = (
    ["--cookies", COOKIES_FILE]
    if os.path.isfile(COOKIES_FILE)
    else []
)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Max simultaneous downloads
DOWNLOAD_WORKERS = asyncio.Semaphore(6)
ACTIVE_DOWNLOADS: Dict[str, asyncio.Lock] = {}
UPLOAD_LOCKS:     Dict[str, asyncio.Lock] = {}

RAM_CACHE = {"audio": {}, "video": {}}

TOKENS       = {}
TOKEN_EXPIRY = 1200  # 20 min


def log(msg): print(msg, flush=True)


# =========================================================
# API KEYS
# =========================================================

REQUIRE_API_KEY = True

# Built-in keys — always valid
API_KEYS = {
    "MusicApi -a9ee0bdff73d3b3f4f8c",
}

# Master password — protects key management + /rebuild_cache
API_MASTER = os.environ.get("API_MASTER", "MusicApi Music@2016").strip()

# Runtime keys created via /genkey (survive restarts)
API_KEYS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "api_keys.json")


def load_dynamic_keys() -> dict:
    if os.path.exists(API_KEYS_FILE):
        try:
            with open(API_KEYS_FILE) as f:
                return json.load(f)
        except Exception as e:
            log(f"⚠️ api_keys.json read error: {e}")
    return {}


def save_dynamic_keys(data: dict):
    try:
        tmp = API_KEYS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, API_KEYS_FILE)
    except Exception as e:
        log(f"⚠️ api_keys.json write error: {e}")


def is_valid_key(key: str) -> bool:
    if not key:
        return False
    return key in API_KEYS or key in load_dynamic_keys()


# =========================================================
# TELEGRAM CONFIG
# =========================================================

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8614226404:AAGHtZn0Cge1AGc4LuT6OLIfgYidxbnQJ74").strip()
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "-1003766108062").strip()
TELEGRAM_API_ID    = int(os.environ.get("TELEGRAM_API_ID", "309262") or "309212")
TELEGRAM_API_HASH  = os.environ.get("TELEGRAM_API_HASH",  "32b313c2830ed817fc8cd502d8975").strip()

TG_MAX_UPLOAD   = 2000 * 1024 * 1024   # 2 GB
TG_DELETE_LOCAL = True                  # remove local file after TG upload

_PYRO_CLIENT  = None
_PYRO_LOCK    = asyncio.Lock()
_pyro_chat_id = None

# telegram_cache.json — maps video_id → {audio: {...}, video: {...}}
TG_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "telegram_cache.json")

# =========================================================
# PO TOKEN PROVIDER — fixes "Sign in to confirm you're not a bot".
# Run the bgutil provider on the server (see header). yt-dlp uses it
# automatically; if it's not running, yt-dlp falls back to cookies.
# =========================================================
# =========================================================
# YT-DLP FLAGS
# =========================================================

YTDLP_COMMON = [
    *YTDLP_COOKIES,
    "--force-ipv4",
    "--js-runtimes",       f"deno:{DENO_BIN},node:{NODE_BIN}",
    "--remote-components", "ejs:github",
    "--no-playlist",
    "--socket-timeout",    "15",
    "--retries",           "10",
    "--retry-sleep",       "linear=1:5",
    "--buffer-size",       "16M",
    "--http-chunk-size",   "10M",
    "--no-mtime",
    "--no-write-info-json",
    "--no-write-thumbnail",
    "--sleep-requests",    "1",
]


# =========================================================
# UTILITIES
# =========================================================

def extract_video_id(text: str) -> Optional[str]:
    """Extract an 11-char YouTube video ID from any YouTube URL, or None."""
    if not text:
        return None
    text = str(text).strip().split("#")[0]
    if re.fullmatch(r"[a-zA-Z0-9_-]{11}", text):
        return text
    m = re.search(r"[?&]v=([a-zA-Z0-9_-]{11})(?=[?&\s]|$)", text)
    if m:
        return m.group(1)
    m = re.search(r"(?:youtu\.be/|/shorts/|/live/|/embed/|/v/)([a-zA-Z0-9_-]{11})(?=[?&/\s]|$)", text)
    if m:
        return m.group(1)
    return None


def parse_input(text: str) -> dict:
    """
    {"platform": "youtube", "id": "VIDEO_ID"}   ← YouTube URL / ID
    {"platform": "search",  "query": "name"}    ← plain text
    None if empty
    """
    if not text:
        return None
    text = str(text).strip()
    vid = extract_video_id(text)
    if vid:
        return {"platform": "youtube", "id": vid}
    return {"platform": "search", "query": text}


def cleanup_tokens():
    now = time.time()
    for t in [k for k, v in TOKENS.items() if now - v.get("time", 0) > TOKEN_EXPIRY]:
        TOKENS.pop(t, None)


def validate_token(token, video_id, media_type):
    cleanup_tokens()
    if not token:
        raise HTTPException(403, "Invalid token")
    data = TOKENS.get(token)
    if not data:
        raise HTTPException(403, "Invalid or expired token")
    if data.get("video_id") != video_id or data.get("type") != media_type:
        raise HTTPException(403, "Token mismatch")
    if time.time() - data.get("time", 0) > TOKEN_EXPIRY:
        TOKENS.pop(token, None)
        raise HTTPException(403, "Token expired")
    return data


def calc_speed(mb, sec):
    return "instant" if sec < 0.01 else f"{mb / sec:.2f} MB/s"


def cleanup_temp_files(key):
    for pat in [f"{CACHE_DIR}/{key}_temp.*", f"{CACHE_DIR}/{key}_temp*"]:
        for p in glob.glob(pat):
            try:
                if os.path.isfile(p):
                    os.remove(p)
            except Exception:
                pass


async def run_cmd(cmd) -> bool:
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join([os.path.dirname(DENO_BIN), os.path.dirname(YTDLP_BIN), os.path.dirname(NODE_BIN), env.get("PATH", "")])
    env["HOME"] = "/root"
    proc = await asyncio.create_subprocess_exec(
        *cmd, env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    out, stderr = await proc.communicate()
    if out:
        print(out.decode(errors="ignore"), flush=True)
    if stderr:
        print(stderr.decode(errors="ignore"), flush=True)
    return proc.returncode == 0


async def run_cmd_out(cmd):
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join([os.path.dirname(DENO_BIN), os.path.dirname(YTDLP_BIN), os.path.dirname(NODE_BIN), env.get("PATH", "")])
    env["HOME"] = "/root"
    proc = await asyncio.create_subprocess_exec(
        *cmd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        log(f"⚠️ Command failed: {err.decode(errors="ignore")[-1000:]}")
    return proc.returncode == 0, out


# =========================================================
# API KEY DEPENDENCY
# =========================================================

def require_api_key(
    api_key:   Optional[str] = Header(None, alias="X-API-Key"),
    key_query: Optional[str] = Query(None,  alias="api_key"),
):
    if not REQUIRE_API_KEY:
        return {"key": None}
    key = api_key or key_query
    if not is_valid_key(key):
        raise HTTPException(403, "Invalid or missing API key")
    return {"key": key}


def _check_master(master: str):
    if not master or master != API_MASTER:
        raise HTTPException(403, "Wrong master password")


# =========================================================
# TELEGRAM CACHE  (telegram_cache.json)
# Every upload also writes caption "MusicApi :ID:TYPE" so /rebuild_cache works.
# =========================================================

def tg_enabled() -> bool:
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def _load_tg_cache() -> dict:
    if os.path.exists(TG_CACHE_FILE):
        try:
            with open(TG_CACHE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_tg_cache(data: dict):
    try:
        tmp = TG_CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, TG_CACHE_FILE)
    except Exception as e:
        log(f"⚠️ TG cache write error: {e}")


def tg_cache_get(key: str, media_type: str) -> Optional[dict]:
    if not tg_enabled():
        return None
    return _load_tg_cache().get(key, {}).get(media_type)


def tg_cache_set(key: str, media_type: str, entry: dict):
    data = _load_tg_cache()
    data.setdefault(key, {})[media_type] = entry
    _save_tg_cache(data)


# =========================================================
# TELEGRAM — Pyrogram USER client (large uploads / streaming)
# =========================================================

async def get_pyro_client():
    global _PYRO_CLIENT, _pyro_chat_id
    if _PYRO_CLIENT is not None:
        return _PYRO_CLIENT
    if not (TELEGRAM_API_ID and TELEGRAM_API_HASH):
        log("⚠️ Pyrogram not configured (TELEGRAM_API_ID / TELEGRAM_API_HASH missing)")
        return None
    async with _PYRO_LOCK:
        if _PYRO_CLIENT is not None:
            return _PYRO_CLIENT
        try:
            from pyrogram import Client
            client = Client("MusicApi _user_uploader", api_id=TELEGRAM_API_ID,
                            api_hash=TELEGRAM_API_HASH,
                            workdir=os.path.dirname(os.path.abspath(__file__)))
            await client.start()
            _pyro_chat_id = int(TELEGRAM_CHAT_ID)
            _PYRO_CLIENT  = client
            log("✅ Pyrogram USER uploader started")
            await _pyro_resolve_peer(client)
            return _PYRO_CLIENT
        except Exception as e:
            log(f"⚠️ Pyrogram start failed: {e}")
            return None


async def _pyro_resolve_peer(client) -> bool:
    cid = int(_pyro_chat_id or TELEGRAM_CHAT_ID)
    try:
        chat = await client.get_chat(cid)
        log(f"✅ Pyrogram resolved: {chat.title or chat.id} | {chat.id}")
        return True
    except Exception:
        pass
    try:
        async for d in client.get_dialogs():
            if d.chat and int(d.chat.id) == cid:
                log(f"✅ Pyrogram peer found in dialogs: {d.chat.id}")
                return True
    except Exception:
        pass
    log(f"⚠️ Pyrogram could not resolve peer {cid}")
    return False


# =========================================================
# TELEGRAM — upload
# =========================================================

async def _upload_bot_api(key, media_type, file_path):
    method  = "sendAudio" if media_type == "audio" else "sendVideo"
    field   = "audio"     if media_type == "audio" else "video"
    mime    = "audio/mp4" if media_type == "audio" else "video/mp4"
    caption = f"MusicApi :{key}:{media_type}"
    async with httpx.AsyncClient(timeout=300) as hx:
        with open(file_path, "rb") as fh:
            r = await hx.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}",
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                files={field: (os.path.basename(file_path), fh, mime)})
    j = r.json()
    if not j.get("ok"):
        log(f"⚠️ TG Bot API upload failed: {j.get('description')}")
        return None
    res  = j["result"]
    f_id = (res.get("audio") or res.get("video") or {}).get("file_id")
    return {"file_id": f_id, "message_id": res.get("message_id")} if f_id else None


async def _upload_pyrogram(key, media_type, file_path):
    client = await get_pyro_client()
    if not client:
        return None
    caption = f"MusicApi :{key}:{media_type}"

    async def _send(cid):
        if media_type == "audio":
            m = await client.send_audio(chat_id=cid, audio=file_path, title=key, caption=caption)
            if m and m.audio:
                return {"file_id": m.audio.file_id, "message_id": m.id}
        else:
            m = await client.send_video(chat_id=cid, video=file_path,
                                        supports_streaming=True, caption=caption)
            if m and m.video:
                return {"file_id": m.video.file_id, "message_id": m.id}
        return None

    try:
        return await _send(_pyro_chat_id)
    except Exception as e:
        if "PEER_ID_INVALID" in str(e).upper():
            if await _pyro_resolve_peer(client):
                try:
                    return await _send(_pyro_chat_id)
                except Exception as e2:
                    log(f"⚠️ Pyrogram retry failed: {e2}")
        else:
            log(f"⚠️ Pyrogram upload error: {e}")
        return None


async def tg_upload(key, media_type, file_path) -> Optional[str]:
    """Upload to Telegram + save entry in cache. Returns file_id or None."""
    if not tg_enabled():
        return None
    existing = tg_cache_get(key, media_type)
    if existing and existing.get("file_id"):
        return existing["file_id"]

    lock = UPLOAD_LOCKS.setdefault(f"{key}:{media_type}", asyncio.Lock())
    async with lock:
        existing = tg_cache_get(key, media_type)
        if existing and existing.get("file_id"):
            return existing["file_id"]

        size = os.path.getsize(file_path)
        if size > TG_MAX_UPLOAD:
            log(f"📦 TG skip (too large): {key}")
            return None
        try:
            if size <= 20 * 1024 * 1024:
                result = await _upload_bot_api(key, media_type, file_path) \
                    or await _upload_pyrogram(key, media_type, file_path)
            else:
                result = await _upload_pyrogram(key, media_type, file_path) \
                    or await _upload_bot_api(key, media_type, file_path)
            if not result or not result.get("file_id"):
                return None
            tg_cache_set(key, media_type, {
                "file_id":    result["file_id"],
                "message_id": result.get("message_id"),
                "size":       size,
                "time":       time.time(),
            })
            log(f"📦 TG uploaded: {key} {media_type} ({size // 1024 // 1024} MB)")
            return result["file_id"]
        except Exception as e:
            log(f"⚠️ TG upload error: {e}")
            return None


# =========================================================
# TELEGRAM — serve
# =========================================================

async def tg_cdn_url(file_id: str) -> Optional[str]:
    try:
        async with httpx.AsyncClient(timeout=20) as hx:
            r = await hx.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile",
                             params={"file_id": file_id})
            j = r.json()
            if j.get("ok") and j["result"].get("file_path"):
                return f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{j['result']['file_path']}"
    except Exception as e:
        log(f"⚠️ TG getFile error: {e}")
    return None


async def tg_stream(file_id, file_size, mime, filename, request=None):
    client = await get_pyro_client()
    if not client:
        return None
    CHUNK = 1024 * 1024
    total = max(0, int(file_size))
    start, end, status = 0, total - 1, 200
    headers = {"Accept-Ranges": "bytes", "Content-Type": mime,
               "Content-Disposition": f'inline; filename="{filename}"'}
    if request:
        rh = request.headers.get("range", "")
        if rh.startswith("bytes="):
            try:
                left, right = rh[6:].split(",", 1)[0].strip().split("-", 1)
                start = int(left) if left else max(0, total - int(right))
                end   = int(right) if right and left else total - 1
                end   = min(end, total - 1)
                if start < 0 or start >= total or end < start:
                    raise ValueError
                status = 206
                headers["Content-Range"] = f"bytes {start}-{end}/{total}"
            except Exception:
                return StreamingResponse(iter(()), status_code=416,
                                         headers={"Content-Range": f"bytes */{total}"}, media_type=mime)
    length = end - start + 1

    async def _iter():
        skip, sent = start % CHUNK, 0
        needed = (skip + length + CHUNK - 1) // CHUNK
        try:
            async for chunk in client.stream_media(file_id, offset=start // CHUNK, limit=needed):
                if skip:
                    chunk = chunk[skip:]; skip = 0
                if len(chunk) > length - sent:
                    chunk = chunk[:length - sent]
                if chunk:
                    sent += len(chunk); yield chunk
                if sent >= length:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"⚠️ TG stream error: {e}")

    log(f"🚀 TG STREAM: {filename} | {total / 1024 / 1024:.1f} MB | {start}-{end}")
    return StreamingResponse(_iter(), status_code=status, headers=headers, media_type=mime)


async def serve_from_tg(entry, mime, filename, request=None):
    file_id   = entry["file_id"]
    file_size = int(entry.get("size", 0) or 0)
    if file_size <= 20 * 1024 * 1024:
        url = await tg_cdn_url(file_id)
        if url:
            return RedirectResponse(url)
    return await tg_stream(file_id, file_size, mime, filename, request)


# =========================================================
# JIOSAAVN — cookie-free, proxy-free, no-block audio (primary for names)
# =========================================================

_JIO_CALL = "https://www.jiosaavn.com/api.php"
_JIO_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json"}
_JIO_DES_KEY = b"38346591"


def _jio_decrypt(enc, quality="320") -> Optional[str]:
    if not _JIOSAAVN_OK:
        return None
    try:
        dec = unpad(DES.new(_JIO_DES_KEY, DES.MODE_ECB).decrypt(base64.b64decode(enc)), 8).decode("utf-8")
        dec = re.sub(r"_(?:12|48|96|160|320)\.mp4", f"_{quality}.mp4", dec)
        return "https://" + dec[7:] if dec.startswith("http://") else dec
    except Exception:
        return None


async def _jio_api(client, params):
    params = {"_format": "json", "_marker": "0", "cc": "in", **params}
    r = await client.get(_JIO_CALL, params=params, headers=_JIO_HEADERS, timeout=20)
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        m = re.search(r"\{.*\}", r.text.strip(), re.S)
        return json.loads(m.group(0)) if m else {}


def _jio_tokens(s):
    return {w for w in re.sub(r"[^\w\s]", " ", str(s or "").lower()).split() if len(w) > 1}


async def _jio_search_best(client, query):
    data = await _jio_api(client, {
        "__call": "search.getResults",
        "q": query,
        "p": "1",
        "n": "10"
    })
    results = (data or {}).get("results") or []
    if not results:
        return None

    def clean_tokens(text):
        text = str(text or "").lower()
        text = re.sub(
            r"\\b(video|song|official|audio|hd|4k|lyrics|lyric|full|"
            r"video\\s*song|official\\s*video|original|remix|"
            r"from|the|feat|ft)\\b",
            " ",
            text
        )
        return {
            w for w in re.sub(r"[^a-z0-9\\s]", " ", text).split()
            if len(w) > 1
        }

    q = str(query or "").strip()
    qtok = clean_tokens(q)

    # YouTube titles usually contain metadata after "|".
    # Give the actual song-title part more importance.
    q_main = q.split("|")[0].strip()
    qmain_tok = clean_tokens(q_main)

    scored = []

    for r in results:
        title = str(r.get("title") or r.get("song") or "").strip()
        if not title:
            continue

        ttok = clean_tokens(title)
        if not ttok:
            continue

        common = qmain_tok & ttok if qmain_tok else qtok & ttok

        # Strong exact/containment match.
        qmain_norm = re.sub(r"[^a-z0-9]+", " ", q_main.lower()).strip()
        title_norm = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()

        exact_bonus = 0
        if qmain_norm and (qmain_norm in title_norm or title_norm in qmain_norm):
            exact_bonus = 1.0

        overlap = len(common) / max(1, len(qmain_tok or qtok))
        coverage = len(common) / max(1, len(ttok))

        score = (overlap * 0.65) + (coverage * 0.20) + (exact_bonus * 0.15)
        scored.append((score, overlap, title, r))

    if not scored:
        return None

    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best_overlap, best_title, best = scored[0]

    # Reject weak matches so a different song is never silently played.
    min_overlap = 0.60 if len(qmain_tok or qtok) >= 3 else 0.50

    if best_overlap < min_overlap:
        log(f"⚠️ JioSaavn weak match rejected: {best_title} | score={best_score:.2f}")
        return None

    log(f"🎯 JioSaavn match accepted: {best_title} | score={best_score:.2f}")
    return best


async def _jio_song_details(client, sid):
    data = await _jio_api(client, {"__call": "song.getDetails", "pids": sid})
    if isinstance(data, dict):
        if sid in data:
            return data[sid]
        if data.get("songs"):
            return data["songs"][0]
    return None


async def jio_resolve(query, quality="320"):
    if not _JIOSAAVN_OK:
        return None, None
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            hit = await _jio_search_best(client, query)
            if not hit:
                return None, None
            sid = hit.get("id")
            song = await _jio_song_details(client, sid) if sid else hit
            if not song:
                return None, None
            more = song.get("more_info") or {}
            enc = more.get("encrypted_media_url") or song.get("encrypted_media_url")
            url = _jio_decrypt(enc, quality) if enc else None
            if not url:
                return None, None
            return url, {"id": song.get("id"), "title": song.get("title") or song.get("song")}
    except Exception as e:
        log(f"⚠️ JioSaavn error: {e}")
        return None, None


async def jio_download(query) -> Optional[str]:
    """Download a song from JioSaavn by name. Returns local path or None."""
    url, meta = await jio_resolve(query)
    if not url:
        return None
    key = "JIO_" + re.sub(r"[^a-zA-Z0-9]", "_", (meta or {}).get("id") or query)[:60]
    out_path = f"{CACHE_DIR}/{key}.m4a"
    if os.path.exists(out_path):
        return out_path
    log(f"🎵 JioSaavn: {query} → {(meta or {}).get('title')}")
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            async with client.stream("GET", url, headers=_JIO_HEADERS, timeout=60) as resp:
                resp.raise_for_status()
                with open(out_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(131072):
                        f.write(chunk)
        if os.path.getsize(out_path) > 0:
            log(f"✅ JioSaavn ok: {os.path.getsize(out_path)//1024} KB")
            return out_path
        os.remove(out_path)
    except Exception as e:
        log(f"⚠️ JioSaavn download failed: {e}")
        if os.path.exists(out_path):
            try:
                os.remove(out_path)
            except Exception:
                pass
    return None


# =========================================================
# YOUTUBE SEARCH (name → top video id, fallback when JioSaavn misses)
# =========================================================

async def yt_search_first(query) -> Optional[str]:
    log(f"🔍 YouTube search: {query}")
    ok, out = await run_cmd_out([
        YTDLP_BIN, *YTDLP_COMMON,
        "--flat-playlist", "--print", "%(id)s",
        "--playlist-items", "1", f"ytsearch1:{query}",
    ])
    if out:
        vid = out.decode(errors="ignore").strip().split("\n")[0].strip()
        if re.fullmatch(r"[a-zA-Z0-9_-]{11}", vid):
            log(f"  → found {vid}")
            return vid
    return None


# =========================================================
# YOUTUBE DOWNLOAD (audio)
# =========================================================

async def yt_download_audio(video_id: str):
    await DOWNLOAD_WORKERS.acquire()
    t0 = time.time()
    out_path = f"{CACHE_DIR}/{video_id}.m4a"

    try:
        cleanup_temp_files(video_id)
        tmp = f"{CACHE_DIR}/{video_id}_temp.%(ext)s"

        cmd = [
            YTDLP_BIN,
            *YTDLP_COOKIES,
            "--js-runtimes", f"deno:{DENO_BIN}",
            "--remote-components", "ejs:github",
            "--no-playlist",
            "--no-part",
            "--no-continue",
            "-f", "bestaudio[ext=m4a]/bestaudio/best",
            "-o", tmp,
            f"https://youtu.be/{video_id}",
        ]

        ok = await run_cmd(cmd)

        hits = [
            f for f in glob.glob(f"{CACHE_DIR}/{video_id}_temp.*")
            if os.path.isfile(f) and os.path.getsize(f) > 0
        ]

        if ok and hits:
            src = max(hits, key=os.path.getsize)

            if os.path.exists(out_path):
                os.remove(out_path)

            shutil.move(src, out_path)
            RAM_CACHE["audio"][video_id] = out_path

            mb = os.path.getsize(out_path) / 1024 / 1024
            elapsed = time.time() - t0

            log(f"✅  Audio ok: {mb:.1f} MB | {elapsed:.1f}s | {calc_speed(mb, elapsed)}")

            cleanup_temp_files(video_id)
            return out_path

        cleanup_temp_files(video_id)

    except Exception as e:
        log(f"❌  Audio exception: {e}")

    finally:
        DOWNLOAD_WORKERS.release()

    log(f"❌  Audio download failed: {video_id}")
    return None

async def yt_download_video(video_id: str) -> Optional[str]:
    if video_id in RAM_CACHE["video"]:
        p = RAM_CACHE["video"][video_id]
        if os.path.exists(p):
            return p
        RAM_CACHE["video"].pop(video_id, None)

    out_path = f"{CACHE_DIR}/{video_id}.mp4"

    if os.path.exists(out_path):
        RAM_CACHE["video"][video_id] = out_path
        log(f"⚡  Video disk cache: {video_id}")
        return out_path

    t0 = time.time()
    log(f"🎬 Video download: {video_id}")

    try:
        await asyncio.wait_for(DOWNLOAD_WORKERS.acquire(), timeout=60)
    except asyncio.TimeoutError:
        raise HTTPException(429, "Too many downloads — please try again")

    try:
        cleanup_temp_files(video_id)
        tmp = f"{CACHE_DIR}/{video_id}_temp.%(ext)s"

        cmd = [
            YTDLP_BIN,
            *YTDLP_COOKIES,
            "--js-runtimes", f"deno:{DENO_BIN}",
            "--remote-components", "ejs:github",
            "--no-playlist",
            "--no-part",
            "--no-continue",
            "-f",
            "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]",
            "--merge-output-format", "mp4",
            "-o", tmp,
            f"https://youtu.be/{video_id}",
        ]

        ok = await run_cmd(cmd)

        hits = [
            f for f in glob.glob(f"{CACHE_DIR}/{video_id}_temp*")
            if os.path.isfile(f)
            and os.path.getsize(f) > 0
            and f.endswith((".mp4", ".mkv", ".webm"))
        ]

        if ok and hits:
            src = max(hits, key=os.path.getsize)

            if os.path.exists(out_path):
                os.remove(out_path)

            shutil.move(src, out_path)
            RAM_CACHE["video"][video_id] = out_path

            mb = os.path.getsize(out_path) / 1024 / 1024
            elapsed = time.time() - t0

            log(f"✅  Video ok: {mb:.1f} MB | {elapsed:.1f}s | {calc_speed(mb, elapsed)}")

            cleanup_temp_files(video_id)
            return out_path

        cleanup_temp_files(video_id)

    finally:
        DOWNLOAD_WORKERS.release()

    log(f"❌  Video download failed: {video_id}")
    return None

# =========================================================
# CORE: serve a YouTube video_id (cache → download → upload)
# =========================================================

async def serve_media(video_id: str, media_type: str, request: Request = None):
    mime     = "audio/mp4" if media_type == "audio" else "video/mp4"
    ext      = ".m4a"      if media_type == "audio" else ".mp4"
    filename = f"{video_id}{ext}"

    # 1. Telegram cache
    if tg_enabled():
        entry = tg_cache_get(video_id, media_type)
        if entry and entry.get("file_id"):
            log(f"⚡ TG cache hit: {video_id} [{media_type}]")
            resp = await serve_from_tg(entry, mime, filename, request)
            if resp is not None:
                return resp
            log(f"⚠️ TG serve failed, re-downloading: {video_id}")

    # 2. Download → upload → serve
    lock = ACTIVE_DOWNLOADS.setdefault(f"{video_id}:{media_type}", asyncio.Lock())
    async with lock:
        if tg_enabled():
            entry = tg_cache_get(video_id, media_type)
            if entry and entry.get("file_id"):
                resp = await serve_from_tg(entry, mime, filename, request)
                if resp is not None:
                    return resp

        path = await (yt_download_audio(video_id) if media_type == "audio"
                      else yt_download_video(video_id))
        if not path:
            raise HTTPException(404, "Download failed")

        file_id = await tg_upload(video_id, media_type, path)
        if file_id:
            entry = tg_cache_get(video_id, media_type)
            if entry:
                resp = await serve_from_tg(entry, mime, filename, request)
                if resp is not None:
                    if TG_DELETE_LOCAL:
                        try:
                            os.remove(path)
                            RAM_CACHE[media_type].pop(video_id, None)
                            log(f"🧹 Local removed: {video_id}")
                        except Exception:
                            pass
                    return resp

        return FileResponse(path, media_type=mime, filename=filename)


async def serve_jio(query: str, request: Request = None):
    """Serve a JioSaavn song by name (audio). Returns response or None if not found."""
    jio_key = "JIO_" + re.sub(r"[^a-zA-Z0-9]", "_", query)[:60]
    entry = tg_cache_get(jio_key, "audio")
    if entry and entry.get("file_id"):
        log(f"⚡ TG cache hit: {jio_key} [audio]")
        resp = await serve_from_tg(entry, "audio/mp4", f"{jio_key}.m4a", request)
        if resp:
            return resp

    path = await jio_download(query)
    if not path:
        return None

    real_key = os.path.splitext(os.path.basename(path))[0]
    file_id = await tg_upload(real_key, "audio", path)
    if file_id:
        e = tg_cache_get(real_key, "audio")
        if e:
            resp = await serve_from_tg(e, "audio/mp4", f"{real_key}.m4a", request)
            if resp:
                if TG_DELETE_LOCAL:
                    try:
                        os.remove(path)
                    except Exception:
                        pass
                return resp
    return FileResponse(path, media_type="audio/mp4", filename=f"{real_key}.m4a")


# =========================================================
# ENDPOINTS
# =========================================================

@app.get("/")
async def root():
    return JSONResponse({"Status": "Music API Running ✅ By KapilYadav"})


@app.get("/status")
async def status():
    return JSONResponse({"Status": "Music API Running ✅ By KapilYadav"})


@app.get("/download")
async def download(
    request:  Request,
    url:      str,
    type:     str = "audio",
    api_key:  Optional[str] = Query(None),
    _auth:    dict = Depends(require_api_key),
):
    cleanup_tokens()
    if type not in ("audio", "video"):
        raise HTTPException(400, "type must be audio or video")

    parsed = parse_input(url)
    if not parsed:
        raise HTTPException(400, "Empty query")

    # ── SONG NAME → JioSaavn (audio) first, then YouTube ──────────────
    if parsed["platform"] == "search":
        query = parsed["query"]
        if type == "audio":
            resp = await serve_jio(query, request)
            if resp is not None:
                return resp
            log(f"🔍 JioSaavn miss → YouTube: {query}")
        # video, or JioSaavn missed → YouTube search
        yt_id = await yt_search_first(query)
        if not yt_id:
            raise HTTPException(404, "Song not found")
        return await serve_media(yt_id, type, request)

    # ── YouTube URL / ID ─────────────────────────────────────────────
    video_id = parsed["id"]

    # Bare YouTube ID: resolve title via oEmbed → JioSaavn first.
    # Full YouTube URLs remain direct YouTube downloads.
    if type == "audio" and not re.search(r"https?://", str(url)):
        oembed_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
        ok, out = await run_cmd_out([
            "curl", "-L", "-sS", "--max-time", "10",
            oembed_url
        ])
        if out:
            try:
                data = json.loads(out.decode(errors="ignore"))
                yt_title = str(data.get("title", "")).strip()
            except Exception:
                yt_title = ""

            if yt_title:
                log(f"🎵 YouTube ID → title: {yt_title}")
                log(f"🔄 Trying JioSaavn first: {yt_title}")
                resp = await serve_jio(yt_title, request)
                if resp is not None:
                    log("✅ JioSaavn success for YouTube ID")
                    return resp
                log(f"🔍 JioSaavn miss → YouTube: {video_id}")

    return await serve_media(video_id, type, request)


@app.get("/stream/{video_id}")
async def stream(
    video_id: str,
    type:     str,
    request:  Request,
    token:    Optional[str] = Query(None),
    X_Download_Token: Optional[str] = Header(None),
    _auth:    dict = Depends(require_api_key),
):
    validate_token(token or X_Download_Token, video_id, type)
    if type not in ("audio", "video"):
        raise HTTPException(400, "Invalid type")
    return await serve_media(video_id, type, request)


# =========================================================
# KEY MANAGEMENT
# =========================================================

@app.get("/genkey")
async def genkey(master: str = Query(...), name: str = Query("user")):
    _check_master(master)
    key  = "MusicApi -" + secrets.token_hex(12)
    data = load_dynamic_keys()
    data[key] = {"name": name, "created": time.time()}
    save_dynamic_keys(data)
    log(f"🔑 Key created for '{name}': {key}")
    return JSONResponse({"status": "success", "key": key, "name": name})


@app.get("/keys")
async def list_keys(master: str = Query(...)):
    _check_master(master)
    data = load_dynamic_keys()
    return JSONResponse({"builtin": sorted(API_KEYS),
                         "generated": [{"key": k, **v} for k, v in data.items()]})


@app.get("/delkey")
async def delkey(master: str = Query(...), key: str = Query(...)):
    _check_master(master)
    data = load_dynamic_keys()
    if key not in data:
        raise HTTPException(404, "Key not found (built-in keys cannot be deleted)")
    del data[key]
    save_dynamic_keys(data)
    log(f"🗑️ Key deleted: {key}")
    return JSONResponse({"status": "success", "deleted": key})


# =========================================================
# REBUILD CACHE (after VPS migration — no re-downloads)
# =========================================================

@app.get("/rebuild_cache")
async def rebuild_cache(master: str = Query(...)):
    _check_master(master)
    client = await get_pyro_client()
    if not client:
        raise HTTPException(503, "Pyrogram not available")

    log("🔄 Rebuilding cache from Telegram group…")
    found, skipped = 0, 0
    data = _load_tg_cache()
    async for msg in client.get_chat_history(int(_pyro_chat_id)):
        caption = (getattr(msg, "caption", None) or "").strip()
        if not caption.startswith("MusicApi :"):
            skipped += 1; continue
        parts = caption.split(":")
        if len(parts) != 3:
            skipped += 1; continue
        _, key, mtype = parts
        if mtype not in ("audio", "video") or (key in data and mtype in data[key]):
            skipped += 1; continue
        media   = getattr(msg, mtype, None)
        file_id = getattr(media, "file_id", None) if media else None
        if not file_id:
            skipped += 1; continue
        data.setdefault(key, {})[mtype] = {
            "file_id":    file_id,
            "message_id": int(msg.id),
            "size":       int(getattr(media, "file_size", 0) or 0),
            "time":       msg.date.timestamp() if msg.date else time.time(),
        }
        found += 1
    _save_tg_cache(data)
    total = sum(len(v) for v in data.values())
    log(f"🔄 Done — {found} indexed, {skipped} skipped, {total} total")
    return JSONResponse({"status": "success", "indexed": found,
                         "skipped": skipped, "total_cached": total})


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
