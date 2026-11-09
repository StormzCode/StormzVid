# StormzVid

A small, beginner-friendly video downloader built with **Python, Flask, HTML, CSS and vanilla JavaScript**.
Paste a link, pick a quality, download the file. It uses [`yt-dlp`](https://github.com/yt-dlp/yt-dlp) to read
and fetch videos and [FFmpeg](https://ffmpeg.org/) to merge video + audio and convert to MP3.

> **Use responsibly.** Only download content you own, content that is in the public domain or openly licensed,
> or content you have explicit permission to save. You are responsible for respecting copyright and each
> platform's terms of service.

## Features

- **One downloader per platform.** The home page is the YouTube downloader and only accepts YouTube links.
  Every other platform lives on its own page (for example `/tiktok`) that only accepts that platform's links.
- **Explore page** (button in the top right) lists 8 supported platforms with icons and a search box.
- Paste a link from the wrong platform and you get a clear message with a button to the right downloader.
- Dark theme by default with violet accents, plus a light theme (toggle in the header, remembered between visits)
- Responsive layout that works on phones, tablets and desktops
- Shows the title, thumbnail, duration and available quality options up to 4K (2160p), with approximate sizes when known
- Loading indicators and clear, human-readable error messages
- Real download flow: the server downloads and processes the file, hands it to your browser, then deletes it
- Your own logo, favicon and platform icons just by dropping files into a folder (see below)

## Pages

| URL | What it is |
| --- | --- |
| `/` | YouTube downloader |
| `/explore` | All platforms, with search |
| `/tiktok`, `/instagram`, `/facebook`, `/snapchat`, `/twitter`, `/reddit`, `/twitch` | One downloader per platform |

## Add your icons

Put image files in `static/icons/` (full guide in `static/icons/README.txt`). File names are not case-sensitive.
Best: square PNG with a transparent background, 128x128 or 256x256 px. `.svg`, `.webp` and `.jpg` work too.
Refresh the page after adding a file, no restart needed. Any icon you haven't added yet shows a coloured letter tile.

| File | Used for |
| --- | --- |
| `Stormz.png` | Your logo in the header and footer (also the tab icon if there's no `.ico`) |
| `stormz.ico` | Browser-tab icon (favicon) |
| `yt.png` | YouTube |
| `ig.png` | Instagram |
| `tiktok.png` | TikTok |
| `fb.png` | Facebook |
| `snap.png` | Snapchat |
| `x.png` | X (Twitter) |
| `reddit.png` | Reddit |
| `twitch.png` | Twitch |

Platform icons sit on a light tile so dark logos (X, TikTok) stay visible in dark mode.

## Safety and limits

- **URL validation**: only `http(s)` links, no embedded credentials, max length 2048
- **Strict per-platform domains**: each page accepts only its own platform's domains (see `PLATFORMS` in `app.py`).
  The server enforces this, not just the browser.
- **File size limit**: 500 MB by default (`STORMZVID_MAX_MB`)
- **Single videos only**: playlists and profiles are never downloaded in bulk
- **Concurrency cap**: 2 simultaneous downloads by default
- **Cleanup**: each download lives in its own temp folder that is deleted right after it is served; unclaimed files are removed after 10 minutes, and the temp folder is wiped on startup
- **No database, no accounts, no cookies, no tracking**

## Requirements

- Python 3.9 or newer
- FFmpeg (recommended, see below)

## Install

```bash
# 1. Get the project and enter the folder
cd stormzvid

# 2. Create and activate a virtual environment
python -m venv venv
source venv/bin/activate          # macOS / Linux
venv\Scripts\activate             # Windows (Command Prompt)
.\venv\Scripts\Activate.ps1       # Windows (PowerShell)

# 3. Install the dependencies
pip install -r requirements.txt
```

### FFmpeg

StormzVid uses the bundled FFmpeg binary from `imageio-ffmpeg` to merge separate HD video and audio
streams (including 4K) and convert audio to MP3. A system FFmpeg installation is optional; when
available, it is preferred.

If you prefer to install FFmpeg yourself, make sure `ffmpeg` is on your `PATH`.

## Run locally

```bash
python app.py
```

Open <http://127.0.0.1:5000> in your browser.

### Optional settings (environment variables)

| Variable | Default | Meaning |
| --- | --- | --- |
| `STORMZVID_MAX_MB` | `500` | Maximum size of a single download |
| `STORMZVID_JOB_TTL` | `600` | Seconds an unclaimed finished file is kept |
| `STORMZVID_MAX_CONCURRENT` | `2` | Simultaneous downloads allowed |
| `FLASK_DEBUG` | `0` | Set to `1` for auto-reload while developing |

Example (macOS / Linux): `STORMZVID_MAX_MB=200 python app.py`
Windows PowerShell: `$env:STORMZVID_MAX_MB=200; python app.py`

## How it works

```
Browser                         Flask (app.py)                     yt-dlp + FFmpeg
  |  POST /api/info  {url, platform}  -> validate URL for THAT platform -> read metadata (no download)
  |  <- title, thumbnail, duration, formats
  |  POST /api/prepare {url, platform, quality} -> download to temp folder -> merge / convert to MP4 or MP3
  |  <- one-time token
  |  GET  /api/file/<token>  -> stream the file, then delete the temp folder
```

## Project structure

```
stormzvid/
├── app.py              # Flask app: platform list, validation, yt-dlp calls, download + cleanup
├── requirements.txt
├── README.md
├── templates/
│   ├── base.html       # Header (Explore button), footer, favicon
│   ├── index.html      # Downloader page (used for every platform)
│   ├── explore.html    # Platform grid with search
│   └── _macros.html    # Platform icon + logo with fallbacks
└── static/
    ├── style.css       # Dark/light themes and layout
    ├── script.js       # Theme, Explore search, fetch info, format picker, download flow
    └── icons/          # Your logo, favicon and platform icons go here
```

## Add another platform

1. Make sure yt-dlp supports it (`yt-dlp --list-extractors`).
2. Add one `_p(...)` line to `PLATFORMS` in `app.py` (name, domains, icon file name, colour, short description).
3. Drop `<icon>.png` into `static/icons/`. The Explore card and its own page appear automatically.

## Which platforms actually work?

StormzVid never pretends a platform works. It asks yt-dlp, and if extraction fails you see an error.
In practice:

- **YouTube, TikTok, Reddit, X and Twitch**: usually work for public videos.
- **Instagram, Facebook and Snapchat** (tagged "Limited"): often require a login or block server-side
  requests. Snapchat only works for public Spotlight links.
- Private, age-restricted, region-locked or DRM-protected videos cannot be downloaded.

Platforms change their sites often. If something that used to work stops working, update yt-dlp first:

```bash
pip install -U yt-dlp
```

## Troubleshooting

- **"FFmpeg is required"**: install FFmpeg and make sure `ffmpeg -version` works in the same terminal.
- **"Server is busy"**: another download is in progress. Wait a moment or raise `STORMZVID_MAX_CONCURRENT`.
- **Thumbnail missing**: some platforms block hot-linked images. The download still works.
- **Icon not showing**: check the file name matches the table above and is inside `static/icons/`. If your browser
  still shows the old tab icon, hard-refresh (Ctrl+F5); browsers cache favicons aggressively.
- **Works locally but not when hosted**: many platforms block datacenter IP addresses.

## Ideas to extend it

- Subtitle download
- A simple per-IP rate limit (e.g. `Flask-Limiter`)
- Progress updates using Server-Sent Events
- Serving with `gunicorn` or `waitress` instead of the Flask development server
