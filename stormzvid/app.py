"""
StormzVid - a small, beginner-friendly video downloader built with Flask and yt-dlp.

Pages
-----
/               YouTube downloader (accepts YouTube links only)
/explore        grid of every supported platform
/<platform>     one downloader per platform, e.g. /tiktok (accepts that platform's links only)

How the download flow works
---------------------------
1. POST /api/info     -> validates the URL for the chosen platform, asks yt-dlp for metadata
                         (title, thumbnail, duration, qualities). Nothing is downloaded yet.
2. POST /api/prepare  -> downloads the chosen quality into a private temp folder
                         (FFmpeg merges video+audio or converts to MP3) and returns a token.
3. GET  /api/file/<t> -> streams the finished file to the browser, then deletes the temp folder.
                         Unclaimed files are also deleted after a short TTL.

Only use this with content you own or have permission to download.
"""

import logging
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
import imageio_ffmpeg
from flask import (Flask, abort, jsonify, redirect, render_template, request,
                   send_file, send_from_directory, url_for)
from yt_dlp.utils import DownloadError, ExtractorError

# --------------------------------------------------------------------------- #
# Configuration (override with environment variables)
# --------------------------------------------------------------------------- #
MAX_FILE_MB = int(os.environ.get("STORMZVID_MAX_MB", "500"))  # per-download size limit
MAX_BYTES = MAX_FILE_MB * 1024 * 1024
JOB_TTL_SECONDS = int(os.environ.get("STORMZVID_JOB_TTL", "600"))  # unclaimed files live 10 min
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("STORMZVID_MAX_CONCURRENT", "2"))
MAX_URL_LENGTH = 2048
MAX_VIDEO_HEIGHT = 2160

BASE_DIR = Path(tempfile.gettempdir()) / "stormzvid"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024  # requests are tiny JSON bodies
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stormzvid")

FFMPEG_PATH = shutil.which("ffmpeg") or imageio_ffmpeg.get_ffmpeg_exe()
HAS_FFMPEG = bool(FFMPEG_PATH and Path(FFMPEG_PATH).is_file())


# --------------------------------------------------------------------------- #
# Platform registry
#
# One entry per platform. Each platform gets its own downloader page that ONLY accepts
# links from its own domains. To add a platform: add a line here (it must be supported
# by yt-dlp) and drop an icon called "<icon>.png" into static/icons/.
#   icon    -> file name (without extension) looked up in static/icons/
#   note    -> shown on the platform's page and card (use it for honest limitations)
#   limited -> shows a "Limited" tag, because these platforms often block downloads
# --------------------------------------------------------------------------- #
def _p(slug, name, domains, icon, color, blurb, note=None, limited=False):
    return {"slug": slug, "name": name, "domains": domains, "icon": icon,
            "color": color, "blurb": blurb, "note": note, "limited": limited}


PLATFORMS = {p["slug"]: p for p in [
    _p("youtube", "YouTube", ["youtube.com", "youtu.be", "youtube-nocookie.com"], "yt",
       "#ff0033", "Videos, Shorts and live replays"),
    _p("instagram", "Instagram", ["instagram.com"], "ig", "#e1306c",
       "Public reels and video posts",
       "Instagram often blocks downloads or asks for a login, so many links will fail.", True),
    _p("tiktok", "TikTok", ["tiktok.com"], "tiktok", "#fe2c55",
       "Public TikTok videos"),
    _p("facebook", "Facebook", ["facebook.com", "fb.watch", "fb.com"], "fb", "#1877f2",
       "Public videos and reels",
       "Facebook often blocks downloads or asks for a login, so many links will fail.", True),
    _p("snapchat", "Snapchat", ["snapchat.com"], "snap", "#f5b400",
       "Public Spotlight videos",
       "Only public Spotlight links can work. Stories and private snaps can't be downloaded.", True),
    _p("twitter", "X (Twitter)", ["twitter.com", "x.com"], "x", "#6b7280",
       "Videos from public posts"),
    _p("reddit", "Reddit", ["reddit.com", "redd.it"], "reddit", "#ff4500",
       "Videos from Reddit posts"),
    _p("twitch", "Twitch", ["twitch.tv"], "twitch", "#9146ff",
       "Past broadcasts and clips"),
]}
FEATURED = ["youtube", "instagram", "tiktok", "facebook", "snapchat"]  # shown in the home strip


def detect_platform(url):
    """Return the platform slug for a URL, or None if the domain isn't allowed."""
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    for slug, p in PLATFORMS.items():
        for domain in p["domains"]:
            if host == domain or host.endswith("." + domain):
                return slug
    return None


def platform_url(slug):
    return url_for("home") if slug == "youtube" else url_for("platform_page", slug=slug)


# --------------------------------------------------------------------------- #
# Icons (drop your own files into static/icons/, see static/icons/README.txt)
# --------------------------------------------------------------------------- #
ICON_DIR = Path(app.static_folder) / "icons"
ICON_EXTS = ("png", "svg", "webp", "jpg", "jpeg")


def find_icon(basename, exts=ICON_EXTS):
    """Find static/icons/<basename>.<ext>, ignoring upper/lower case. Returns file name or None."""
    if not ICON_DIR.is_dir():
        return None
    want = basename.lower()
    for f in sorted(ICON_DIR.iterdir()):
        stem, _, ext = f.name.rpartition(".")
        if stem.lower() == want and ext.lower() in exts:
            return f.name
    return None


def _icon_url(filename):
    # ?v=<mtime> makes the browser pick up a replaced icon right away
    version = int((ICON_DIR / filename).stat().st_mtime)
    return url_for("static", filename=f"icons/{filename}", v=version)


def icon_for(slug):
    """URL of a platform's icon, or None (the templates then show a coloured letter tile)."""
    p = PLATFORMS.get(slug)
    name = find_icon(p["icon"]) if p else None
    return _icon_url(name) if name else None


def brand_icon():
    """URL of the StormzVid logo (static/icons/Stormz.png) or None."""
    name = find_icon("stormz", ("png",))
    return _icon_url(name) if name else None


def favicon_ico():
    """URL of the browser-tab icon (static/icons/stormz.ico) or None."""
    name = find_icon("stormz", ("ico",))
    return _icon_url(name) if name else None


app.jinja_env.globals.update(icon_for=icon_for, brand_icon=brand_icon,
                             favicon_ico=favicon_ico, platform_url=platform_url)

# Finished downloads waiting to be picked up: token -> {path, dir, filename, created}
JOBS = {}
JOBS_LOCK = threading.Lock()
DOWNLOAD_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_DOWNLOADS)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
class UrlError(ValueError):
    """A problem with the pasted link. `suggest` is the slug of the page it belongs on, if any."""

    def __init__(self, message, suggest=None):
        super().__init__(message)
        self.suggest = suggest


def validate_url(raw, slug):
    """Return a clean URL that belongs to platform `slug`, or raise UrlError."""
    platform = PLATFORMS[slug]
    url = (raw or "").strip()
    if not url:
        raise UrlError("Please paste a video link first.")
    if len(url) > MAX_URL_LENGTH:
        raise UrlError("That link is too long.")
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise UrlError("That doesn't look like a valid link.")
    if parsed.username or parsed.password:
        raise UrlError("Links containing credentials are not allowed.")

    detected = detect_platform(url)
    if detected == slug:
        return url
    if detected:
        other = PLATFORMS[detected]["name"]
        raise UrlError(f"This is a {other} link, but this page only accepts {platform['name']} links.",
                       suggest=detected)
    raise UrlError(f"This doesn't look like a {platform['name']} link. Paste a {platform['name']} "
                   f"link, or open Explore to pick another platform.")


def url_error_response(exc):
    body = {"error": str(exc)}
    if exc.suggest:
        body["suggest"] = {"name": PLATFORMS[exc.suggest]["name"], "url": platform_url(exc.suggest)}
    return jsonify(body), 400


def friendly_error(exc):
    """Turn a noisy yt-dlp exception into a short message for the user."""
    raw = re.sub(r"\x1b\[[0-9;]*m", "", str(exc))  # strip ANSI colours
    msg = raw.lower()
    log.info("yt-dlp error: %s", raw[:300])
    if "unsupported url" in msg:
        return "This link isn't a video that can be extracted. Check that it points to a single video."
    if "max-filesize" in msg or "larger than" in msg:
        return f"This video is larger than the {MAX_FILE_MB} MB limit. Try a lower quality."
    if "requested format is not available" in msg or "requested format not available" in msg:
        return "That quality is no longer available. Get the link again to refresh the available options."
    if any(w in msg for w in ("http error 403", "http error 410", "forbidden", "signature extraction")):
        return "The platform rejected this download request. Update yt-dlp and try again later."
    if "ffmpeg" in msg or "ffprobe" in msg:
        return "FFmpeg is required for this option but wasn't found on the server."
    if any(w in msg for w in ("private", "login", "log in", "sign in", "cookies", "confirm your age", "age-restricted")):
        return "This video is private, age-restricted, or requires a login, so it can't be downloaded."
    if any(w in msg for w in ("geo", "not available in your country", "region")):
        return "This video isn't available in the server's region."
    if any(w in msg for w in ("429", "too many requests", "rate limit", "rate-limit")):
        return "The platform is limiting requests right now. Please try again in a few minutes."
    if any(w in msg for w in ("unavailable", "removed", "deleted", "not exist", "404", "no video")):
        return "This video is unavailable or has been removed."
    return ("Couldn't get this video. The platform may block downloads or may not be supported "
            "right now. Try again later.")


def format_filename(title, ext):
    """Safe download name: no path characters, limited length."""
    safe = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "", title or "").strip(" .")[:80]
    return f"{safe or 'video'}.{ext.lstrip('.')}"


def base_opts():
    return {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "playlist_items": "1",  # never grab whole playlists / profiles
        "socket_timeout": 20,
        "retries": 2,
    }


def pick_entry(info):
    """If yt-dlp returned a playlist wrapper, use its first entry."""
    if info and info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        return entries[0] if entries else None
    return info


def size_of(fmt):
    return fmt.get("filesize") or fmt.get("filesize_approx") or 0


def build_formats(info):
    """Summarise yt-dlp's format list into a few simple choices for the UI."""
    formats = info.get("formats") or []
    audio_sizes = [size_of(f) for f in formats
                   if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")]
    best_audio = max(audio_sizes, default=0)

    by_height = {}
    for f in formats:
        height = f.get("height")
        if not height or f.get("vcodec") in (None, "none"):
            continue
        size = size_of(f)
        if size and f.get("acodec") in (None, "none"):
            size += best_audio  # video-only stream: audio gets merged in
        if not HAS_FFMPEG and f.get("acodec") in (None, "none"):
            continue
        if height not in by_height or size > by_height[height]:
            by_height[height] = size

    options = [
        {"quality": str(h), "label": "4K (2160p)" if h == 2160 else f"{h}p",
         "size": by_height[h] or None}
        for h in sorted(by_height, reverse=True)
        if 100 <= h <= MAX_VIDEO_HEIGHT
    ]
    has_eligible_video = any(
        f.get("vcodec") not in (None, "none")
        and (HAS_FFMPEG or f.get("acodec") not in (None, "none"))
        and (f.get("height") is None or f["height"] <= MAX_VIDEO_HEIGHT)
        for f in formats
    )
    direct_video_is_eligible = info.get("url") and (
        info.get("height") is None or info["height"] <= MAX_VIDEO_HEIGHT
    )
    if not options and (has_eligible_video or direct_video_is_eligible):
        options.append({"quality": "best", "label": "Best available", "size": None})
    options.append({
        "quality": "audio",
        "label": "MP3 audio" if HAS_FFMPEG else "Audio only",
        "size": best_audio or None,
    })
    return options


def download_opts(workdir, quality):
    """yt-dlp options for a given quality choice ('audio', 'best' or a height up to 2160)."""
    opts = base_opts()
    opts.update({
        "outtmpl": str(workdir / "download.%(ext)s"),
        "max_filesize": MAX_BYTES,
        "format_sort": ["res", "ext:mp4:m4a"],  # prefer MP4/M4A so files play everywhere
        "merge_output_format": "mp4",
        "ffmpeg_location": FFMPEG_PATH,
    })
    if quality == "audio":
        if HAS_FFMPEG:
            opts["format"] = "ba/b"
            opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]
        else:
            opts["format"] = "ba[ext=m4a]/ba/b"
    else:
        height = MAX_VIDEO_HEIGHT if quality == "best" else int(quality)
        flt = f"[height<={height}]"
        if HAS_FFMPEG:
            opts["format"] = f"bv*{flt}+ba/b{flt}"
        else:
            opts["format"] = f"b{flt}"  # single pre-merged file, no FFmpeg needed
    return opts


def cleanup_expired_jobs():
    """Delete finished downloads that nobody collected."""
    now = time.time()
    with JOBS_LOCK:
        stale = [t for t, j in JOBS.items() if now - j["created"] > JOB_TTL_SECONDS]
        jobs = [JOBS.pop(t) for t in stale]
    for job in jobs:
        shutil.rmtree(job["dir"], ignore_errors=True)


def reset_temp_dir():
    """Start with a clean temp folder (removes leftovers from a previous crash)."""
    shutil.rmtree(BASE_DIR, ignore_errors=True)
    BASE_DIR.mkdir(parents=True, exist_ok=True)


def requested_platform(data):
    """The platform slug sent by the page, or None if it isn't a known platform."""
    slug = str(data.get("platform", "")).lower()
    return slug if slug in PLATFORMS else None


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
def render_downloader(slug):
    # Small map the browser uses to spot "wrong platform" links while you type.
    platform_map = {s: {"name": p["name"], "domains": p["domains"], "url": platform_url(s)}
                    for s, p in PLATFORMS.items()}
    return render_template("index.html", p=PLATFORMS[slug], platform_map=platform_map,
                           featured=[PLATFORMS[s] for s in FEATURED],
                           extra_count=len(PLATFORMS) - len(FEATURED),
                           max_mb=MAX_FILE_MB, year=datetime.now().year)


@app.get("/")
def home():
    return render_downloader("youtube")


@app.get("/explore")
def explore():
    return render_template("explore.html", platforms=list(PLATFORMS.values()),
                           max_mb=MAX_FILE_MB, year=datetime.now().year)


@app.get("/favicon.ico")
def favicon():
    """Browsers ask for /favicon.ico by default; serve stormz.ico (or Stormz.png) if present."""
    ico = find_icon("stormz", ("ico",))
    if ico:
        return send_from_directory(ICON_DIR, ico, mimetype="image/x-icon")
    png = find_icon("stormz", ("png",))
    if png:
        return send_from_directory(ICON_DIR, png)
    return "", 204


@app.get("/<slug>")
def platform_page(slug):
    slug = slug.lower()
    if slug not in PLATFORMS:
        abort(404)
    if slug == "youtube":
        return redirect(url_for("home"))
    return render_downloader(slug)


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@app.post("/api/info")
def api_info():
    data = request.get_json(silent=True) or {}
    slug = requested_platform(data)
    if not slug:
        return jsonify(error="Unknown platform."), 400
    try:
        url = validate_url(data.get("url"), slug)
    except UrlError as exc:
        return url_error_response(exc)

    try:
        with yt_dlp.YoutubeDL(base_opts()) as ydl:
            info = pick_entry(ydl.extract_info(url, download=False))
    except (DownloadError, ExtractorError) as exc:
        return jsonify(error=friendly_error(exc)), 422
    except Exception:
        log.exception("Unexpected error while reading %s", url)
        return jsonify(error="Something went wrong on our side. Please try again."), 500

    if not info or not (info.get("formats") or info.get("url")):
        return jsonify(error="No downloadable video was found at this link."), 422

    return jsonify(
        url=url,
        platform=slug,
        platform_name=PLATFORMS[slug]["name"],
        title=info.get("title") or "Untitled video",
        uploader=info.get("uploader") or info.get("channel") or "",
        thumbnail=info.get("thumbnail") or "",
        duration=info.get("duration"),
        formats=build_formats(info),
        max_mb=MAX_FILE_MB,
    )


@app.post("/api/prepare")
def api_prepare():
    data = request.get_json(silent=True) or {}
    slug = requested_platform(data)
    if not slug:
        return jsonify(error="Unknown platform."), 400
    try:
        url = validate_url(data.get("url"), slug)
    except UrlError as exc:
        return url_error_response(exc)

    quality = str(data.get("quality", "best"))
    if not (quality in ("best", "audio") or
            (quality.isdigit() and 100 <= int(quality) <= MAX_VIDEO_HEIGHT)):
        return jsonify(error="Invalid quality option."), 400

    cleanup_expired_jobs()
    if not DOWNLOAD_SLOTS.acquire(blocking=False):
        return jsonify(error="The server is busy with other downloads. Please try again in a moment."), 429

    workdir = Path(tempfile.mkdtemp(dir=BASE_DIR))
    keep = False
    try:
        with yt_dlp.YoutubeDL(download_opts(workdir, quality)) as ydl:
            info = pick_entry(ydl.extract_info(url, download=True))

        files = [p for p in workdir.iterdir()
                 if p.is_file() and p.suffix not in (".part", ".ytdl", ".temp")]
        if not files:
            # yt-dlp quietly skips files over max_filesize, so this is the usual cause.
            return jsonify(error=f"No file was created. It may exceed the {MAX_FILE_MB} MB limit "
                                 "or the selected format may no longer be available. Try a lower "
                                 "quality or get the link again."), 422
        output = max(files, key=lambda p: p.stat().st_size)

        if output.stat().st_size > MAX_BYTES:
            return jsonify(error=f"The file is larger than the {MAX_FILE_MB} MB limit. Try a lower quality."), 413

        token = secrets.token_urlsafe(24)
        filename = format_filename((info or {}).get("title"), output.suffix)
        with JOBS_LOCK:
            JOBS[token] = {"path": str(output), "dir": str(workdir),
                           "filename": filename, "created": time.time()}
        keep = True
        return jsonify(token=token, filename=filename, size=output.stat().st_size)

    except (DownloadError, ExtractorError) as exc:
        return jsonify(error=friendly_error(exc)), 422
    except Exception:
        log.exception("Unexpected error while downloading %s", url)
        return jsonify(error="Something went wrong while processing the video."), 500
    finally:
        DOWNLOAD_SLOTS.release()
        if not keep:
            shutil.rmtree(workdir, ignore_errors=True)


@app.get("/api/file/<token>")
def api_file(token):
    with JOBS_LOCK:
        job = JOBS.pop(token, None)  # one-time use
    if not job or not Path(job["path"]).exists():
        if job:
            shutil.rmtree(job["dir"], ignore_errors=True)
        return jsonify(error="This download has expired. Please start again."), 404

    response = send_file(job["path"], as_attachment=True, download_name=job["filename"])
    # send_file() sets direct_passthrough, which skips Response.close() and would silently
    # prevent the cleanup callback below from ever running. Turn it off so it does run.
    response.direct_passthrough = False
    response.call_on_close(lambda: shutil.rmtree(job["dir"], ignore_errors=True))
    return response


reset_temp_dir()

if __name__ == "__main__":
    if not HAS_FFMPEG:
        log.warning("FFmpeg not found. Merging HD video and MP3 conversion are limited. "
                    "See README.md for install steps.")
    app.run(host="127.0.0.1", port=5000, debug=os.environ.get("FLASK_DEBUG") == "1")
