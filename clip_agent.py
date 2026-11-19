#!/usr/bin/env python3
"""
Clip Agent - a free, local YouTube-to-Shorts clipper.

  1. Pulls the video's transcript (YouTube captions, or Whisper for local files)
  2. Has an AI (free Gemini API key, local Ollama, or a pasted plan) pick the
     best moments using a clipping playbook: hooks, titles, Part 1/Part 2
  3. Downloads ONLY the chosen sections and renders 9:16 clips with a title
     band, hook text, word-by-word captions and Part labels.

Command line:
  python clip_agent.py find   <youtube-url-or-file> [--clips 10] [--length 40]
  python clip_agent.py render <path/to/clip_plan.json> [--only 1,3,5]
  python clip_agent.py run    <youtube-url-or-file>      (find + render)
Or launch the app:  python app.py
"""
from __future__ import annotations

import argparse
import difflib
import json
import math
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

try:  # some Python installs (python.org on macOS without "Install Certificates.command") ship with
    import certifi, ssl  # noqa: E401 - no system CA bundle, so urllib.request.urlopen fails every HTTPS call
    ssl._create_default_https_context = lambda: ssl.create_default_context(cafile=certifi.where())
except Exception:
    pass

HERE = Path(__file__).resolve().parent
FONTS_DIR = HERE / "fonts"
CONFIG_PATH = HERE / "config.json"
OUT_ROOT = HERE / "output"
MODELS_DIR = HERE / "models"

# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
_log_hook = None


def set_log_hook(fn):
    global _log_hook
    _log_hook = fn


def log(msg: str):
    print(msg, flush=True)
    if _log_hook:
        try:
            _log_hook(msg)
        except Exception:
            pass


def hms(sec: float) -> str:
    sec = max(0, int(round(sec)))
    return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


def parse_ts(v) -> float:
    """'01:02:03', '62:03', '3723', 3723.5 -> seconds."""
    if v is None:
        raise ValueError("missing timestamp")
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return float(s)
    parts = [float(p) for p in s.split(":")]
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total


def slugify(text: str, n: int = 40) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return (s[:n].strip("-")) or "clip"


def load_config() -> dict:
    cfg = {"provider": "gemini", "gemini_api_key": "", "gemini_model": "",
           "ollama_model": "llama3.1:8b", "ollama_host": "http://localhost:11434", "cookies_browser": "",
           "whisper_fallback": True}
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    if os.environ.get("GEMINI_API_KEY"):
        cfg["gemini_api_key"] = os.environ["GEMINI_API_KEY"]
    return cfg


def save_config(cfg: dict):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def ffmpeg_exe() -> str:
    p = shutil.which("ffmpeg")
    if p:
        return p
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        raise RuntimeError("ffmpeg not found. Run the setup script again (it installs imageio-ffmpeg).")


NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # keep ffmpeg from flashing console windows on Windows


def run_ffmpeg(args: list[str], cwd: Path | None = None):
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y"] + args
    r = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg failed:\n" + r.stderr[-2000:])
    return r


def media_duration(path: Path) -> float:
    r = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", r.stderr)
    if not m:
        return 0.0
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


COOKIES_FILE = HERE / "cookies.txt"
_COOKIES_BROKEN = False  # set when the browser's login can't be read; then we carry on without it


def ytdlp_base_opts(use_cookies: bool = True) -> dict:
    """Shared yt-dlp options. To get past 'confirm you're not a bot' blocks, Settings can give yt-dlp the
    user's YouTube login: an exported cookies.txt file (works with any browser) or a browser to read from."""
    opts = {"quiet": True, "no_warnings": True, "noprogress": True,
            "socket_timeout": 45, "retries": 10, "fragment_retries": 10, "extractor_retries": 3,
            "file_access_retries": 5}
    if use_cookies and not _COOKIES_BROKEN:
        if COOKIES_FILE.exists():
            opts["cookiefile"] = str(COOKIES_FILE)
        else:
            browser = (load_config().get("cookies_browser") or "").strip().lower()
            if browser and browser != "none":
                opts["cookiesfrombrowser"] = (browser,)
    return opts


def _is_cookie_error(e: Exception) -> bool:
    t = (str(e) + " " + type(e).__name__).lower()
    return any(k in t for k in ("cookie", "dpapi", "decrypt", "app-bound", "keyring"))


def run_ytdlp(fn):
    """Run fn(opts). If reading the YouTube login fails (Chrome/Edge often block this - on the Mac it
    usually means a Keychain prompt for "Chrome Safe Storage" was denied), retry without it."""
    global _COOKIES_BROKEN
    try:
        return fn(ytdlp_base_opts())
    except Exception as e:
        if not _is_cookie_error(e) or _COOKIES_BROKEN:
            raise
        _COOKIES_BROKEN = True
        log("Couldn't read your YouTube login from the browser (Chrome and Edge often block this). "
            "Continuing without it. Settings has a way that works with Firefox.")
        return fn(ytdlp_base_opts())


def is_url(s: str) -> bool:
    return bool(re.match(r"https?://", (s or "").strip()))


# --------------------------------------------------------------------------
# transcript
# --------------------------------------------------------------------------
def parse_json3(data: dict) -> list[dict]:
    """YouTube json3 captions -> [{'s','e','w'}]. Auto captions carry per-word offsets."""
    words = []
    for ev in data.get("events", []):
        segs = ev.get("segs")
        if not segs:
            continue
        t0 = ev.get("tStartMs", 0) / 1000.0
        dur = ev.get("dDurationMs", 0) / 1000.0
        items = []
        for sg in segs:
            txt = sg.get("utf8", "")
            if not txt.strip():
                continue
            items.append((t0 + sg.get("tOffsetMs", 0) / 1000.0, txt))
        if not items:
            continue
        has_offsets = any("tOffsetMs" in sg for sg in segs)
        spread = (not has_offsets) or (len(items) == 1 and len(items[0][1].split()) > 1)
        if spread:
            toks = " ".join(t for _, t in items).split()
            step = (dur if dur > 0 else 0.4 * len(toks)) / max(len(toks), 1)
            for i, tk in enumerate(toks):
                words.append({"s": t0 + i * step, "e": t0 + (i + 1) * step, "w": tk})
        else:
            for ts, txt in items:
                for tk in txt.split():
                    words.append({"s": ts, "e": None, "w": tk})
    words.sort(key=lambda x: x["s"])
    for i, w in enumerate(words):
        if w["e"] is None:
            nxt = words[i + 1]["s"] if i + 1 < len(words) else w["s"] + 0.6
            w["e"] = max(w["s"] + 0.05, min(nxt, w["s"] + 1.2))
    return words


def youtube_info(url: str) -> dict:
    """Video info only (title, length, caption track list) - one light request."""
    from yt_dlp import YoutubeDL

    def go(opts):
        opts.update({"skip_download": True})
        with YoutubeDL(opts) as ydl:
            return ydl.sanitize_info(ydl.extract_info(url, download=False))
    return run_ytdlp(go)


def _meta_from_info(info: dict, url: str) -> dict:
    return {"id": info.get("id"), "title": info.get("title") or "video",
            "duration": float(info.get("duration") or 0), "channel": info.get("uploader") or "",
            "url": info.get("webpage_url") or url, "source": url, "is_url": True}


def _is_rate_limit(e: Exception) -> bool:
    t = str(e)
    return ("429" in t or "Too Many Requests" in t or "IpBlocked" in type(e).__name__
            or "RequestBlocked" in type(e).__name__ or _is_network_blip(e))


def _is_network_blip(e: Exception) -> bool:
    """Timeouts and dropped connections: worth retrying."""
    t = (str(e) + " " + type(e).__name__).lower()
    return any(k in t for k in ("timed out", "timeout", "connection reset", "connection aborted", "remote end closed",
                                "remotedisconnected", "temporarily unavailable", "temporary failure",
                                "connection refused", "eof occurred", "incompleteread"))


def _captions_ytdlp(url: str, info: dict, lang: str) -> list[dict]:
    """Download ONE caption track (json3) with polite retries when YouTube rate-limits."""
    from yt_dlp import YoutubeDL
    auto = info.get("automatic_captions") or {}
    subs = info.get("subtitles") or {}
    order = [(auto, f"{lang}-orig"), (subs, lang)] + [(subs, k) for k in subs if k.startswith(lang + "-")] + [(auto, lang)]
    track = None
    for src, key in order:
        for f in src.get(key) or []:
            if f.get("ext") == "json3" and f.get("url"):
                track = (key, f["url"])
                break
        if track:
            break
    if not track:
        return []
    global _COOKIES_BROKEN
    waits = [0, 20, 45, 90]
    ydl = YoutubeDL(ytdlp_base_opts())
    try:
        i = 0
        while i < len(waits):
            w = waits[i]
            if w:
                log(f"YouTube is limiting or slow to respond - waiting {w}s, then retrying ({i}/{len(waits) - 1})...")
                time.sleep(w)
            try:
                raw = ydl.urlopen(track[1]).read().decode("utf-8", errors="ignore")
                return parse_json3(json.loads(raw))
            except Exception as e:
                if _is_cookie_error(e) and not _COOKIES_BROKEN:
                    _COOKIES_BROKEN = True
                    log("Couldn't read your YouTube login from the browser. Continuing without it.")
                    ydl.close()
                    ydl = YoutubeDL(ytdlp_base_opts())
                    continue  # same attempt again, no wait
                if _is_rate_limit(e):
                    i += 1
                    continue
                log(f"Caption download failed ({str(e)[:120]}).")
                return []
    finally:
        ydl.close()
    log("YouTube is still rate-limiting caption downloads.")
    return []


def _captions_transcript_api(video_id: str, lang: str) -> list[dict]:
    """Second route to the same captions (different YouTube endpoint)."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except Exception:
        return []
    for attempt, w in enumerate([0, 30]):
        if w:
            log(f"Waiting {w}s before trying the backup caption route again...")
            time.sleep(w)
        try:
            fetched = YouTubeTranscriptApi().fetch(video_id, languages=[lang, f"{lang}-US", f"{lang}-GB"])
            words = []
            for sn in fetched:
                toks = sn.text.replace("\n", " ").split()
                if not toks:
                    continue
                step = max(sn.duration, 0.3 * len(toks)) / len(toks)
                for i, tk in enumerate(toks):
                    words.append({"s": sn.start + i * step, "e": sn.start + (i + 1) * step, "w": tk})
            return words
        except Exception as e:
            if _is_rate_limit(e):
                continue
            log(f"Backup caption route failed ({type(e).__name__}).")
            return []
    return []


def _download_audio(url: str, workdir: Path) -> Path:
    from yt_dlp import YoutubeDL
    for old in workdir.glob("audio.*"):
        old.unlink()
    def go(opts):
        opts.update({"format": "ba[ext=m4a]/ba/b", "outtmpl": str(workdir / "audio.%(ext)s"),
                     "ffmpeg_location": ffmpeg_exe()})
        with YoutubeDL(opts) as ydl:
            ydl.download([url])
    run_ytdlp(go)
    files = [f for f in workdir.glob("audio.*") if f.suffix not in (".part", ".ytdl")]
    if not files:
        raise RuntimeError("Audio download failed.")
    return files[0]


_WHISPER_MODELS = {}


def _load_whisper(model_size: str):
    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "120")
    os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "60")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    from faster_whisper import WhisperModel
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    for attempt in range(4):
        try:
            return WhisperModel(model_size, device="cpu", compute_type="int8", download_root=str(MODELS_DIR))
        except Exception as e:
            if attempt == 3 or not _is_network_blip(e):
                raise RuntimeError(f"Couldn't download the speech model ({str(e)[:120]}). "
                                   "Check your internet and press Try again; the download resumes where it stopped.")
            log(f"Speech model download timed out, retrying ({attempt + 1}/3)...")
            time.sleep(5 * (attempt + 1))


def whisper_words(media: Path, model_size: str = "small", offset: float = 0.0, total: float = 0.0) -> list[dict]:
    if model_size not in _WHISPER_MODELS:
        log(f"Loading Whisper '{model_size}' (first time downloads the model)...")
        _WHISPER_MODELS[model_size] = _load_whisper(model_size)
    segs, _ = _WHISPER_MODELS[model_size].transcribe(str(media), word_timestamps=True, vad_filter=True)
    words, last_report, t0 = [], 0.0, time.time()
    for s in segs:
        for w in (s.words or []):
            t = w.word.strip()
            if t:
                words.append({"s": w.start + offset, "e": w.end + offset, "w": t})
        if total and s.end - last_report >= 600:
            last_report = s.end
            rate = s.end / max(1.0, time.time() - t0)
            left = (total - s.end) / max(rate, 0.01)
            log(f"Transcribed {hms(s.end)} of {hms(total)} (about {max(1, round(left / 60))} min left)...")
    return words


def get_transcript(source: str, lang: str = "en", allow_whisper: bool | None = None):
    """Returns (meta, words, workdir). Caches transcript.json so YouTube is only asked once per video."""
    OUT_ROOT.mkdir(exist_ok=True)
    cfg = load_config()
    if allow_whisper is None:
        allow_whisper = cfg.get("whisper_fallback", True)
    if is_url(source):
        log("Reading video info from YouTube...")
        info = youtube_info(source)
        meta = _meta_from_info(info, source)
        workdir = OUT_ROOT / f"{slugify(meta['title'])}_{meta['id']}"
        workdir.mkdir(exist_ok=True)
        cache = workdir / "transcript.json"
        if cache.exists():
            words = json.loads(cache.read_text(encoding="utf-8"))["words"]
            if words:
                log(f"Using the saved transcript ({len(words):,} words).")
                return meta, words, workdir
        log("Getting captions...")
        words = _captions_ytdlp(source, info, lang)
        if not words:
            log("Trying the backup caption route...")
            words = _captions_transcript_api(meta["id"], lang)
        if not words and allow_whisper:
            size = "small" if meta["duration"] <= 3600 else "base"
            est = max(2, round(meta["duration"] / 60 / 8))
            log(f"Captions unavailable, so I'll transcribe the audio myself with Whisper '{size}'. "
                f"Downloading audio first; transcription runs on your computer (rough guess: ~{est} min)...")
            audio = _download_audio(source, workdir)
            words = whisper_words(audio, size, total=meta["duration"])
        if not words:
            raise RuntimeError(
                "Couldn't get captions for this video. YouTube is either rate-limiting you (wait 20-30 minutes, "
                "or set 'use my YouTube login' in Settings) or the video has no captions yet. "
                "You can also turn on 'transcribe the audio myself' in Settings.")
    else:
        path = Path(source).expanduser().resolve()
        if not path.exists():
            raise RuntimeError(f"File not found: {path}")
        workdir = OUT_ROOT / slugify(path.stem)
        workdir.mkdir(exist_ok=True)
        cache = workdir / "transcript.json"
        meta = {"id": slugify(path.stem), "title": path.stem, "duration": media_duration(path),
                "channel": "", "url": "", "source": str(path), "is_url": False}
        if cache.exists():
            words = json.loads(cache.read_text(encoding="utf-8"))["words"]
        else:
            size = "small" if meta["duration"] <= 3600 else "base"
            log(f"Transcribing {hms(meta['duration'])} of audio with Whisper '{size}' - "
                "this runs on your computer and can take a while for long videos...")
            words = whisper_words(path, size, total=meta["duration"])
    (workdir / "transcript.json").write_text(json.dumps({"meta": meta, "words": words}), encoding="utf-8")
    log(f"Transcript ready: {len(words):,} words, {hms(meta['duration'])} long.")
    return meta, words, workdir


def transcript_lines(words, start, end, line_sec=10.0) -> list[str]:
    lines, cur, cur_t = [], [], None
    for w in words:
        if w["s"] < start or w["s"] >= end:
            continue
        if cur_t is None:
            cur_t = w["s"]
        cur.append(w["w"])
        span = w["s"] - cur_t
        if (span >= line_sec and w["w"][-1:] in ".?!,") or span >= line_sec * 1.6:
            lines.append(f"[{hms(cur_t)}] {' '.join(cur)}")
            cur, cur_t = [], None
    if cur:
        lines.append(f"[{hms(cur_t)}] {' '.join(cur)}")
    return lines


# --------------------------------------------------------------------------
# AI: finding clips
# --------------------------------------------------------------------------
PLAYBOOK = """You are an elite short-form video clipper. You turn long videos (podcasts, streams,
interviews, vlogs) into YouTube Shorts / TikTok / Reels clips that go viral.

WHAT MAKES A MOMENT CLIP-WORTHY
- Hot take / contrarian claim said with conviction ("Most people think X, but actually Y").
- Emotional peak: big laughter, vulnerability, anger, shock, awe, a long pause before an answer.
- Surprising reveal: a number, a dollar amount, a secret, "I've never told anyone this".
- A complete mini-story: setup -> tension -> payoff.
- A quotable one-liner that makes sense with zero context.
- Conflict or disagreement between people. Name-drops of famous people/brands.
- Relatable pain points people tag friends in. Funny exchanges, roasts, chaos.
- Transcript tells: "wait", "no way", "[Laughter]", "bro", "honestly", "the truth is", interruptions.
SKIP: moments needing more than ~10s of backstory, rambling, hedging, inside jokes, ads/sponsor reads,
intros/outros, moments whose payoff is outside the clip. Mix clip types (funny, heated, emotional, useful).

RULES FOR EACH CLIP
- Start mid-moment at the most intense line. Never start with greetings, "so", "yeah", or setup filler.
- If the single best line is NOT at the start, give a cold_open: a 1-3 second line from inside the clip
  that will be flashed at the very beginning as a teaser before the clip plays from its start.
- The clip must make sense to a stranger and contain its payoff before it ends.
- Length: the shortest length that delivers the full idea. Target is given below; funny moments can be
  15-30s, stories up to 60s. Only go longer for a story that clearly holds attention.
- End right after the punchline/payoff (or on a line that loops back into the opening). Never trail off.
- title_top: 3-8 words shown at the top of the screen the whole clip. Curiosity, no spoilers, different
  from the spoken first line. e.g. "He didn't expect this answer", "The question that ended the interview".
- hook_text: 2-6 punchy words flashed in the first ~2.5 seconds, e.g. "WAIT FOR IT", "HE WAS SERIOUS",
  "$2M MISTAKE". No emojis.
- PART 1 / PART 2: when a great story needs more than ~70 seconds or has a natural cliffhanger, split it.
  Part 1 must be watchable alone but end at peak tension right BEFORE the resolution. Part 2 delivers the
  payoff and gets a part2_recap: a short line (max 10 words) that re-hooks new viewers, e.g.
  "He just admitted he lost everything - here's what happened next". Never fake a cliffhanger.
- Scores 1-10 each: hook (first line stops the scroll), emotion, standalone (zero-context clarity),
  payoff, shareability (comments/tags/debate).
- start_quote / end_quote: copy the first 5-8 words and last 5-8 words of the clip EXACTLY as written in
  the transcript (these are used to cut precisely)."""

SCHEMA = """Return ONLY JSON in this exact shape:
{"clips": [{
  "start": "HH:MM:SS", "end": "HH:MM:SS",
  "start_quote": "first 5-8 words exactly", "end_quote": "last 5-8 words exactly",
  "cold_open": null or {"start": "HH:MM:SS", "quote": "the 1-3 second teaser line exactly"},
  "type": "hot take|story|funny|emotional|reveal|conflict|advice|other",
  "title_top": "...", "hook_text": "...",
  "series": null or {"id": "s1", "part": 1, "of": 2},
  "part2_recap": null or "...",
  "ending": "loop|hard cut|cliffhanger",
  "scores": {"hook": 0, "emotion": 0, "standalone": 0, "payoff": 0, "shareability": 0},
  "post_caption": "one curiosity line + a question that drives comments",
  "hashtags": ["#..."],
  "why": "one sentence"
}]}"""


def _extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except Exception:
        a, b = text.find("{"), text.rfind("}")
        if a >= 0 and b > a:
            return json.loads(text[a:b + 1])
        raise


# Used only if Google's model list can't be fetched. Normally the list comes from Google (see _gemini_models).
GEMINI_FALLBACKS = ["gemini-3.8-flash", "gemini-flash-latest", "gemini-flash-lite-latest", "gemini-2.5-flash",
                    "gemini-2.5-flash-lite"]
_GEMINI_GOOD: str | None = None          # model that last answered; tried first next time
_GEMINI_DONE_TODAY: dict[str, float] = {}  # model -> time we learned its daily free quota is used up
_GEMINI_GONE: set[str] = set()           # models Google says this key can't use
_GEMINI_LIST: tuple[float, list[str]] | None = None
_SKIP_WORDS = ("image", "tts", "audio", "live", "embedding", "native", "computer", "cyber", "robotics", "vision",
               "learnlm", "thinking-exp", "aqa", "omni")


def _gemini_models(key: str) -> list[str]:
    """Ask Google which text models this key can use; best Flash models first, then Flash-Lite."""
    global _GEMINI_LIST
    if _GEMINI_LIST and time.time() - _GEMINI_LIST[0] < 6 * 3600:
        return _GEMINI_LIST[1]
    try:
        req = urllib.request.Request("https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000",
                                     headers={"x-goog-api-key": key})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
        names = []
        for mdl in data.get("models", []):
            name = mdl.get("name", "").split("/")[-1]
            if "generateContent" not in (mdl.get("supportedGenerationMethods") or []):
                continue
            low = name.lower()
            if "flash" not in low or any(w in low for w in _SKIP_WORDS):
                continue
            names.append(name)

        def rank(n):
            v = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
            ver = float(v.group(1)) if v else 0.0  # "-latest" aliases go after the exact versions
            return ("lite" in n, -ver, "preview" in n or "exp" in n, "latest" in n, n)
        ordered = sorted(set(names), key=rank)
        if ordered:
            _GEMINI_LIST = (time.time(), ordered)
            return ordered
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403):
            detail = e.read().decode(errors="ignore").lower()
            if "api key" in detail or "api_key" in detail or "permission" in detail:
                raise RuntimeError("Gemini rejected the API key. Double-check it in Settings "
                                   "(it should start with 'AIza').")
    except Exception:
        pass
    return GEMINI_FALLBACKS


def _gemini_quota_info(detail: str) -> dict:
    """Read Google's 429 body: is it a per-day limit (waiting won't help) or per-minute (wait a bit)?"""
    info = {"daily": False, "zero": False, "delay": None, "msg": ""}
    try:
        err = json.loads(detail).get("error", {})
    except Exception:
        err = {}
    msg = err.get("message", "") or detail
    info["msg"] = msg.split("\n")[0][:220]
    blob = detail.lower()
    info["daily"] = "perday" in blob or "per day" in blob or "requestsperday" in blob
    info["zero"] = bool(re.search(r"limit:\s*0\b", blob)) or '"quotavalue": "0"' in blob
    m = re.search(r'"retryDelay":\s*"(\d+)', detail) or re.search(r"retry in (\d+)", detail)
    if m:
        info["delay"] = int(m.group(1))
    return info


def gemini_generate(prompt: str, key: str, model: str) -> str:
    global _GEMINI_GOOD
    if not key:
        raise RuntimeError("No Gemini API key. Get a free one at https://aistudio.google.com/apikey "
                           "and paste it into the app's settings.")
    # forget "used up today" notes after 6 hours (quotas reset daily at midnight Pacific time)
    for mm, t in list(_GEMINI_DONE_TODAY.items()):
        if time.time() - t > 6 * 3600:
            _GEMINI_DONE_TODAY.pop(mm, None)
    available = _gemini_models(key)
    preferred = [m for m in [_GEMINI_GOOD, (model or "").strip()] if m]
    order = list(dict.fromkeys(preferred + available))
    order = [m for m in order if m not in _GEMINI_GONE]
    order = [m for m in order if m not in _GEMINI_DONE_TODAY] or order
    unavailable, limited, google_said = [], [], ""
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.6}}
    idx = -1
    while idx + 1 < len(order):
        idx += 1
        m = order[idx]
        nxt = order[idx + 1] if idx + 1 < len(order) else None
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"
        minute_waits = 0
        attempt = 0
        while attempt < 6:
            attempt += 1
            req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json", "x-goog-api-key": key})
            try:
                with urllib.request.urlopen(req, timeout=300) as r:
                    data = json.load(r)
                parts = data["candidates"][0]["content"]["parts"]
                _GEMINI_GOOD = m
                return "".join(p.get("text", "") for p in parts if not p.get("thought"))
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="ignore")
                low = detail.lower()
                if e.code == 429:
                    q = _gemini_quota_info(detail)
                    google_said = q["msg"]
                    if q["daily"] or q["zero"] or minute_waits >= 2:
                        _GEMINI_DONE_TODAY[m] = time.time()
                        limited.append(m)
                        why = "has no free quota on your key" if q["zero"] else "used up its free requests for today"
                        log(f"{m} {why}." + (f" Switching to {nxt}..." if nxt else ""))
                        break
                    wait = min(max(q["delay"] or 20, 5), 60) + 2
                    minute_waits += 1
                    log(f"Gemini's free tier allows only a few requests per minute. Waiting {wait}s...")
                    time.sleep(wait)
                    continue
                if e.code in (500, 502, 503, 504):
                    if attempt >= 3:
                        log(f"{m} is overloaded right now." + (f" Switching to {nxt}..." if nxt else ""))
                        break
                    log(f"Gemini is busy ({e.code}), retrying in {10 * attempt}s...")
                    time.sleep(10 * attempt)
                    continue
                retired = e.code == 404 or (e.code == 400 and "model" in low and any(
                    k in low for k in ("not found", "not supported", "not available", "no longer", "deprecated")))
                if retired:
                    unavailable.append(m)
                    _GEMINI_GONE.add(m)
                    sug = re.search(r"use (?:models/)?(gemini-[\w.\-]+)", detail)
                    if sug and sug.group(1) not in order:
                        order.insert(idx + 1, sug.group(1))
                    nxt = order[idx + 1] if idx + 1 < len(order) else None
                    log(f"{m} isn't available to new Gemini keys anymore." + (f" Using {nxt} instead..." if nxt else ""))
                    if m == (model or "").strip():  # stop asking for it in future runs
                        cfg = load_config()
                        cfg["gemini_model"] = ""
                        save_config(cfg)
                    break
                if e.code in (400, 401, 403) and ("api key" in low or "api_key" in low or "permission" in low):
                    raise RuntimeError("Gemini rejected the API key. Double-check it in Settings "
                                       "(it should start with 'AIza').")
                raise RuntimeError(f"Gemini error {e.code}: {detail[:300]}")
            except (KeyError, IndexError):
                raise RuntimeError(f"Gemini returned no text (it may have blocked the content). Model: {m}")
            except Exception as e:  # timeouts, dropped connections, flaky Wi-Fi
                if not _is_network_blip(e) and not isinstance(e, (OSError, ValueError)):
                    raise
                if attempt >= 5:
                    raise RuntimeError("Couldn't reach Gemini: the connection kept timing out. Check your internet, "
                                       "then press Try again (finished parts are saved).")
                wait = 10 * attempt
                log(f"Gemini didn't answer in time ({str(e)[:60]}). Retrying in {wait}s...")
                time.sleep(wait)
                continue
    if limited:
        raise RuntimeError(
            "Your free Gemini key has used up today's free requests on every free model. Google resets them at "
            "midnight Pacific time (2 AM in Texas). Your progress is saved: press Try again after that. "
            "Or switch to Ollama in Settings, or ask Claude for a clip plan and paste it in."
            + (f" Google said: {google_said}" if google_said else ""))
    raise RuntimeError(f"Google didn't accept any of these Gemini models for your key: {', '.join(unavailable)}. "
                       "Press Check for updates in Settings (a newer version may know the new model names), "
                       "or type a model name from aistudio.google.com into Settings > Advanced.")


def ollama_generate(prompt: str, model: str, host: str) -> str:
    body = {"model": model, "prompt": prompt, "format": "json", "stream": False,
            "options": {"num_ctx": 32768, "temperature": 0.6}}
    req = urllib.request.Request(host.rstrip("/") + "/api/generate", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=1800) as r:
            return json.load(r)["response"]
    except urllib.error.URLError as e:
        raise RuntimeError(f"Couldn't reach Ollama at {host}. Is the Ollama app running? ({e})")


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9']", "", t.lower())


def find_quote(words, quote, around, window=120.0):
    """Fuzzy-find a quote near `around` seconds. Returns (first_word_idx, last_word_idx) or None."""
    q = [_norm(x) for x in (quote or "").split() if _norm(x)]
    if len(q) < 2:
        return None
    idx = [i for i, w in enumerate(words) if around - window <= w["s"] <= around + window]
    if not idx:
        return None
    i0, i1 = idx[0], idx[-1]
    toks = [_norm(words[i]["w"]) for i in range(i0, i1 + 1)]
    n, best_r, best_j = len(q), 0.0, None
    for j in range(0, max(1, len(toks) - n + 1)):
        r = difflib.SequenceMatcher(None, q, toks[j:j + n]).ratio()
        if r > best_r or (r == best_r and best_j is not None and
                          abs(words[i0 + j]["s"] - around) < abs(words[i0 + best_j]["s"] - around)):
            best_r, best_j = r, j
    if best_j is None or best_r < 0.55:
        return None
    return i0 + best_j, min(i0 + best_j + n - 1, len(words) - 1)


def refine_clip(c: dict, words: list[dict], duration: float) -> dict | None:
    try:
        start, end = parse_ts(c.get("start")), parse_ts(c.get("end"))
    except Exception:
        return None
    sq = find_quote(words, c.get("start_quote"), start)
    if sq:
        start = words[sq[0]]["s"]
    eq = find_quote(words, c.get("end_quote"), end)
    if eq and words[eq[1]]["e"] > start + 3:
        end = words[eq[1]]["e"]
    start = max(0.0, start - 0.15)
    end = end + 0.35
    if duration:
        end = min(end, duration)
    if end - start < 4:
        return None
    c["start"], c["end"] = round(start, 2), round(end, 2)
    co = c.get("cold_open")
    if isinstance(co, dict) and co.get("quote"):
        try:
            around = parse_ts(co.get("start", start))
        except Exception:
            around = start
        hit = find_quote(words, co["quote"], around, window=180)
        if hit and words[hit[1]]["e"] - words[hit[0]]["s"] <= 6:
            c["cold_open"] = {"start": round(words[hit[0]]["s"] - 0.05, 2),
                              "end": round(words[hit[1]]["e"] + 0.15, 2), "quote": co["quote"]}
        else:
            c["cold_open"] = None
    else:
        c["cold_open"] = None
    sc = c.get("scores") or {}
    c["score"] = int(sum(int(sc.get(k, 0) or 0) for k in ("hook", "emotion", "standalone", "payoff", "shareability")))
    c["length"] = round(c["end"] - c["start"], 1)
    return c


def _overlap(a, b) -> float:
    inter = max(0.0, min(a["end"], b["end"]) - max(a["start"], b["start"]))
    return inter / max(1e-6, min(a["end"] - a["start"], b["end"] - b["start"]))


def find_clips(meta: dict, words: list[dict], workdir: Path, n_clips: int = 10, target_len: int = 40,
               provider: str = "gemini", cfg: dict | None = None, extra_instructions: str = "") -> dict:
    cfg = cfg or load_config()
    duration = meta.get("duration") or (words[-1]["e"] if words else 0)
    window = 3600.0 if provider == "gemini" else 1200.0
    n_windows = max(1, math.ceil(duration / window))
    per_window = max(3, math.ceil(n_clips * 1.6 / n_windows))
    candidates = []
    import hashlib
    run_key = hashlib.sha1(json.dumps([provider, n_clips, target_len, extra_instructions, window]).encode()).hexdigest()[:10]
    ai_dir = workdir / "ai_sections" / run_key
    ai_dir.mkdir(parents=True, exist_ok=True)
    for wi in range(n_windows):
        a, b = wi * window, min(duration, (wi + 1) * window) + (0 if wi == n_windows - 1 else 0)
        lines = transcript_lines(words, a, b + 60)  # small overlap
        if len(lines) < 5:
            continue
        saved = ai_dir / f"section_{wi:02d}.json"
        if saved.exists():
            try:
                data = json.loads(saved.read_text(encoding="utf-8"))
                log(f"AI is reading {hms(a)} - {hms(b)} ({wi + 1}/{n_windows})... (already done)")
                for c in data.get("clips", []):
                    if isinstance(c, dict):
                        candidates.append(c)
                continue
            except Exception:
                pass
        log(f"AI is reading {hms(a)} - {hms(b)} ({wi + 1}/{n_windows})...")
        prompt = (PLAYBOOK + "\n\n" + SCHEMA +
                  f"\n\nVIDEO: \"{meta.get('title')}\" by {meta.get('channel') or 'unknown'}, total length {hms(duration)}."
                  f"\nThis is the transcript section {hms(a)} to {hms(b)}. Timestamps in [brackets] mark line starts."
                  f"\nFind up to {per_window} of the BEST clip candidates in this section (fewer if the section is weak)."
                  f"\nTarget clip length: about {target_len} seconds."
                  + (f"\nExtra instructions from the user: {extra_instructions}" if extra_instructions else "")
                  + "\n\nTRANSCRIPT:\n" + "\n".join(lines))
        if provider == "gemini":
            text = gemini_generate(prompt, cfg.get("gemini_api_key", ""), cfg.get("gemini_model", ""))
        elif provider == "ollama":
            text = ollama_generate(prompt, cfg.get("ollama_model", "llama3.1:8b"), cfg.get("ollama_host", "http://localhost:11434"))
        else:
            raise RuntimeError(f"Unknown AI provider: {provider}")
        try:
            data = _extract_json(text)
        except Exception:
            log("AI returned something that isn't valid JSON for this section - skipping it.")
            continue
        kept = []
        for c in data.get("clips", []) if isinstance(data, dict) else []:
            if isinstance(c, dict):
                if isinstance(c.get("series"), dict):
                    c["series"]["id"] = f"w{wi}_{c['series'].get('id', 's')}"
                kept.append(c)
        candidates.extend(kept)
        saved.write_text(json.dumps({"clips": kept}), encoding="utf-8")
    return build_plan(meta, words, workdir, candidates, n_clips, target_len)


def build_plan(meta, words, workdir, raw_clips, n_clips=10, target_len=40) -> dict:
    duration = meta.get("duration") or 0
    refined = [r for r in (refine_clip(dict(c), words, duration) for c in raw_clips) if r]
    # rank: series are ranked by their best part and kept together
    groups = {}
    for c in refined:
        sid = (c.get("series") or {}).get("id") if c.get("series") else None
        groups.setdefault(sid or f"single_{id(c)}", []).append(c)
    ranked_groups = sorted(groups.values(), key=lambda g: -max(c["score"] for c in g))
    chosen = []
    for g in ranked_groups:
        g.sort(key=lambda c: (c.get("series") or {}).get("part", 1))
        if any(_overlap(c, k) > 0.3 for c in g for k in chosen):
            continue
        chosen.extend(g)
        if len(chosen) >= n_clips:
            break
    for i, c in enumerate(chosen, 1):
        c["n"] = i
    plan = {"video": {k: meta.get(k) for k in ("id", "title", "channel", "duration", "url", "source", "is_url")},
            "settings": {"target_length": target_len}, "clips": chosen}
    save_plan(plan, workdir)
    log(f"Found {len(chosen)} clips. Plan saved to {workdir / 'clip_plan.md'}")
    return plan


def save_plan(plan: dict, workdir: Path):
    (workdir / "clip_plan.json").write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    (workdir / "clip_plan.md").write_text(plan_markdown(plan), encoding="utf-8")


def plan_markdown(plan: dict) -> str:
    v = plan["video"]
    out = [f"# Clip plan: {v.get('title')}", "",
           f"{len(plan['clips'])} clips from a {hms(v.get('duration') or 0)} video.", ""]
    for c in plan["clips"]:
        s = c.get("series")
        part = f"PART {s['part']}/{s.get('of', 2)}" if s else "Single"
        yt = f"{v.get('url')}&t={int(c['start'])}s" if v.get("url") and "?" in (v.get("url") or "") else ""
        out += [f"## {c['n']}. {c.get('title_top', '')}",
                f"- **Time:** {hms(c['start'])} -> {hms(c['end'])} ({c['length']}s) | **{c.get('type', '')}** | "
                f"**Score {c['score']}/50** | {part}" + (f" | [watch]({yt})" if yt else ""),
                f"- **Hook text (0-2.5s):** {c.get('hook_text', '')}"]
        if c.get("cold_open"):
            out.append(f"- **Cold open teaser:** \"{c['cold_open'].get('quote', '')}\" ({hms(c['cold_open']['start'])})")
        if c.get("part2_recap"):
            out.append(f"- **Part 2 recap line:** {c['part2_recap']}")
        out += [f"- **Opens with:** \"{c.get('start_quote', '')}...\"",
                f"- **Ends with:** \"...{c.get('end_quote', '')}\" ({c.get('ending', '')})",
                f"- **Post caption:** {c.get('post_caption', '')} {' '.join(c.get('hashtags') or [])}",
                f"- **Why it works:** {c.get('why', '')}", ""]
    return "\n".join(out)


def load_plan(path) -> tuple[dict, Path]:
    p = Path(path)
    plan = json.loads(p.read_text(encoding="utf-8"))
    return plan, p.parent


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------
def ass_color(hex_rgb: str, alpha: int = 0) -> str:
    h = hex_rgb.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def ass_time(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    return f"{cs // 360000}:{(cs // 6000) % 60:02d}:{(cs // 100) % 60:02d}.{cs % 100:02d}"


def ass_escape(t: str) -> str:
    return t.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ")


LAYOUTS = {
    # positions are centre points on a 1080x1920 canvas
    "blur": {"part_y": 262, "title_y": 420, "hook_y": 760, "cap_y": 1385, "end_y": 980},
    "fill": {"part_y": 268, "title_y": 410, "hook_y": 820, "cap_y": 1300, "end_y": 980},
    "stack": {"part_y": 268, "title_y": 390, "hook_y": 700, "cap_y": 960, "end_y": 1180},
}

NOISE = re.compile(r"^\[.*\]$|^\(.*\)$|^>>$|^-$")


def caption_groups(words, max_words=3, max_chars=13, gap=0.6):
    groups, cur = [], []
    for w in words:
        t = w["w"].strip()
        if NOISE.match(t):
            continue
        t = t.strip(",.;:\"“”").upper()
        if not t:
            continue
        item = {"s": w["s"], "e": w["e"], "t": t, "raw": w["w"]}
        if cur:
            chars = sum(len(x["t"]) + 1 for x in cur) + len(t)
            if (len(cur) >= max_words or chars > max_chars or w["s"] - cur[-1]["e"] > gap
                    or cur[-1]["raw"].rstrip()[-1:] in ".?!,"):
                groups.append(cur)
                cur = []
        cur.append(item)
    if cur:
        groups.append(cur)
    return groups


def build_ass(clip: dict, words_out: list[dict], total: float, layout: str, highlight: str,
              show_title=True) -> str:
    L = LAYOUTS[layout]
    hl = ass_color(highlight)
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,Poppins Black,100,&H00FFFFFF,&H00FFFFFF,&H00000000,&H90000000,0,0,0,0,100,100,1,0,1,9,4,5,50,50,0,1
Style: Title,Poppins ExtraBold,70,&H00000000,&H00000000,&H00FFFFFF,&H00000000,0,0,0,0,100,100,0,0,3,20,0,5,90,90,0,1
Style: Hook,Poppins Black,86,&H00000000,&H00000000,{ass_color('#FFE500')},&H00000000,0,0,0,0,100,100,1,0,3,18,0,5,80,80,0,1
Style: Part,Poppins Black,50,&H00FFFFFF,&H00FFFFFF,{ass_color('#FF2D55')},&H00000000,0,0,0,0,100,100,2,0,3,12,0,5,80,80,0,1
Style: End,Poppins Black,78,&H00FFFFFF,&H00FFFFFF,{ass_color('#FF2D55')},&H00000000,0,0,0,0,100,100,1,0,3,22,0,5,80,80,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    series = clip.get("series") or None
    if show_title and clip.get("title_top"):
        ev.append(f"Dialogue: 2,{ass_time(0)},{ass_time(total)},Title,,0,0,0,,{{\\pos(540,{L['title_y']})}}{ass_escape(clip['title_top'])}")
    if series:
        label = f"PART {series.get('part', 1)}"
        ev.append(f"Dialogue: 3,{ass_time(0)},{ass_time(total)},Part,,0,0,0,,{{\\pos(540,{L['part_y']})}}{label}")
    hook = clip.get("part2_recap") if series and series.get("part", 1) > 1 and clip.get("part2_recap") else clip.get("hook_text")
    if hook:
        hook_end = min(2.6 if not clip.get("part2_recap") else 3.2, total)
        ev.append(f"Dialogue: 4,{ass_time(0.05)},{ass_time(hook_end)},Hook,,0,0,0,,"
                  f"{{\\pos(540,{L['hook_y']})\\fscx70\\fscy70\\t(0,120,\\fscx100\\fscy100)\\fad(0,180)}}"
                  f"{ass_escape(hook.upper() if len(hook) < 40 else hook)}")
    if series and series.get("part", 1) < series.get("of", 2):
        st = max(0.0, total - 2.2)
        ev.append(f"Dialogue: 5,{ass_time(st)},{ass_time(total)},End,,0,0,0,,"
                  f"{{\\pos(540,{L['end_y']})\\fscx60\\fscy60\\t(0,150,\\fscx100\\fscy100)}}PART {series.get('part', 1) + 1} ON MY PAGE")
    # word-by-word captions
    groups = caption_groups(words_out)
    for gi, g in enumerate(groups):
        g_end = g[-1]["e"]
        if gi + 1 < len(groups) and groups[gi + 1][0]["s"] - g_end < 0.35:
            g_end = groups[gi + 1][0]["s"]
        g_end = min(g_end + 0.08, total)
        for k, w in enumerate(g):
            s = w["s"] if k else g[0]["s"]
            e = g[k + 1]["s"] if k + 1 < len(g) else g_end
            if e <= s:
                e = s + 0.05
            parts = []
            for j, x in enumerate(g):
                if j == k:
                    parts.append(f"{{\\c{hl}\\fscx108\\fscy108}}{ass_escape(x['t'])}{{\\r}}")
                else:
                    parts.append(ass_escape(x["t"]))
            pop = "\\fscx88\\fscy88\\t(0,90,\\fscx100\\fscy100)" if k == 0 else ""
            ev.append(f"Dialogue: 1,{ass_time(s)},{ass_time(e)},Caption,,0,0,0,,"
                      f"{{\\pos(540,{L['cap_y']}){pop}}}" + " ".join(parts))
    return head + "\n".join(ev) + "\n"


def layout_filter(layout: str, crop_x: float = 0.5) -> str:
    if layout == "fill":
        return (f"[vc]scale=1080:1920:force_original_aspect_ratio=increase,"
                f"crop=1080:1920:x=(iw-1080)*{crop_x:.3f}:y=(ih-1920)/2,setsar=1[vl]")
    if layout == "stack":
        return ("[vc]split=2[l][r];"
                "[l]crop=iw/2:ih:0:0,scale=1080:960:force_original_aspect_ratio=increase,crop=1080:960[t];"
                "[r]crop=iw/2:ih:iw/2:0,scale=1080:960:force_original_aspect_ratio=increase,crop=1080:960[b];"
                "[t][b]vstack=inputs=2,setsar=1[vl]")
    # blur: zoomed video in the middle, blurred copy behind it
    return ("[vc]split=2[b0][f0];"
            "[b0]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=10:2,"
            "scale=1080:1920,eq=brightness=-0.10[bg];"
            "[f0]scale=1080:760:force_original_aspect_ratio=increase,crop=1080:760[fg];"
            "[bg][fg]overlay=0:560,setsar=1[vl]")


def fetch_range(meta: dict, start: float, end: float, out_base: Path) -> Path:
    """Get [start, end] of the source as an mp4. Only that section is downloaded."""
    for old in out_base.parent.glob(out_base.name + ".*"):
        old.unlink()
    if meta.get("is_url"):
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import download_range_func
        def go(opts):
            opts.update({
                "format": "bv*[height<=1080][vcodec^=avc1]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/b",
                "download_ranges": download_range_func(None, [(start, end)]),
                "force_keyframes_at_cuts": True, "merge_output_format": "mp4",
                "outtmpl": str(out_base) + ".%(ext)s", "ffmpeg_location": ffmpeg_exe(),
            })
            with YoutubeDL(opts) as ydl:
                ydl.download([meta.get("url") or meta["source"]])
        run_ytdlp(go)
        files = [f for f in out_base.parent.glob(out_base.name + ".*") if f.suffix in (".mp4", ".mkv", ".webm")]
        if not files:
            raise RuntimeError("Download of the clip section failed.")
        return files[0]
    out = out_base.with_suffix(".mp4")
    run_ffmpeg(["-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", meta["source"],
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", "-b:a", "192k", str(out)])
    return out


def render_clip(clip: dict, plan: dict, workdir: Path, words_all: list[dict] | None = None,
                layout: str = "blur", highlight: str = "#FFE500", use_whisper: bool = True,
                tighten: bool = True, crop_x: float = 0.5) -> Path:
    meta = plan["video"]
    work = workdir / "work"
    work.mkdir(exist_ok=True)
    fonts_local = work / "fonts"
    if not fonts_local.exists():
        shutil.copytree(FONTS_DIR, fonts_local)
    clips_dir = workdir / "clips"
    clips_dir.mkdir(exist_ok=True)
    n = clip.get("n", 1)
    start, end = float(clip["start"]), float(clip["end"])
    co = clip.get("cold_open")
    lo = min(start, co["start"]) if co else start
    hi = max(end, co["end"]) if co else end
    pad = 1.0
    raw_start = max(0.0, lo - pad)
    raw_end = hi + pad
    log(f"[{n}] Getting {hms(start)}-{hms(end)} ...")
    raw = fetch_range(meta, raw_start, raw_end, work / f"raw_{n:02d}")

    # word timings, relative to the raw file
    words = None
    if use_whisper:
        try:
            words = whisper_words(raw, "small", offset=0.0)
        except Exception as e:
            log(f"[{n}] Whisper unavailable ({str(e)[:120]}), using YouTube caption timing.")
    if not words:
        words = [{"s": w["s"] - raw_start, "e": w["e"] - raw_start, "w": w["w"]}
                 for w in (words_all or []) if raw_start <= w["s"] <= raw_end]
    rs, re_ = start - raw_start, end - raw_start

    # pieces of the raw file to keep, in output order
    main_words = [w for w in words if rs - 0.05 <= w["s"] < re_]
    pieces = []
    if tighten and len(main_words) > 2:
        seg_a = rs
        for a, b in zip(main_words, main_words[1:]):
            if b["s"] - a["e"] > 0.75:
                pieces.append((seg_a, a["e"] + 0.15))
                seg_a = b["s"] - 0.1
        pieces.append((seg_a, re_))
    else:
        pieces = [(rs, re_)]
    if co:
        pieces = [(co["start"] - raw_start, co["end"] - raw_start)] + pieces
    raw_len = media_duration(raw) or (raw_end - raw_start)
    pieces = [(max(0.0, a), min(raw_len, b)) for a, b in pieces if min(raw_len, b) - max(0.0, a) > 0.2]

    # map words onto the output timeline
    words_out, t = [], 0.0
    for a, b in pieces:
        for w in words:
            if a <= w["s"] < b:
                words_out.append({"s": w["s"] - a + t, "e": min(w["e"], b) - a + t, "w": w["w"]})
        t += b - a
    total = t

    ass_path = work / f"clip_{n:02d}.ass"
    ass_path.write_text(build_ass(clip, words_out, total, layout, highlight), encoding="utf-8")

    fc = []
    for i, (a, b) in enumerate(pieces):
        d = b - a
        fc.append(f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v{i}]")
        fc.append(f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS,"
                  f"afade=t=in:d=0.02,afade=t=out:st={max(0.0, d - 0.03):.3f}:d=0.03[a{i}]")
    fc.append("".join(f"[v{i}][a{i}]" for i in range(len(pieces))) + f"concat=n={len(pieces)}:v=1:a=1[vc][ac]")
    fc.append(layout_filter(layout, crop_x))
    fc.append(f"[vl]subtitles={ass_path.name}:fontsdir=fonts,fps=30,format=yuv420p[vout]")
    fc.append("[ac]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[aout]")
    out = clips_dir / f"{n:02d}_{slugify(clip.get('title_top') or 'clip', 36)}.mp4"
    log(f"[{n}] Rendering {total:.0f}s vertical clip ...")
    run_ffmpeg(["-i", raw.name, "-filter_complex", ";".join(fc), "-map", "[vout]", "-map", "[aout]",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-b:a", "160k",
                "-movflags", "+faststart", str(out.resolve())], cwd=work)
    log(f"[{n}] Done -> {out.name}")
    return out


def render_plan(plan_path, only=None, **kw) -> list[Path]:
    plan, workdir = load_plan(plan_path)
    words_all = None
    tp = workdir / "transcript.json"
    if tp.exists():
        words_all = json.loads(tp.read_text(encoding="utf-8"))["words"]
    outs = []
    for c in plan["clips"]:
        if only and c.get("n") not in only:
            continue
        try:
            outs.append(render_clip(c, plan, workdir, words_all, **kw))
        except Exception as e:
            log(f"[{c.get('n')}] FAILED: {e}")
    return outs


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Free YouTube-to-Shorts clip agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("find", "run"):
        p = sub.add_parser(name)
        p.add_argument("source", help="YouTube link or path to a video file")
        p.add_argument("--clips", type=int, default=10)
        p.add_argument("--length", type=int, default=40, help="target clip length in seconds")
        p.add_argument("--ai", default=None, choices=["gemini", "ollama"])
        p.add_argument("--lang", default="en")
        p.add_argument("--notes", default="", help="extra instructions, e.g. 'only funny moments'")
    p = sub.add_parser("render")
    p.add_argument("plan", help="path to clip_plan.json")
    p.add_argument("--only", default="", help="clip numbers, e.g. 1,3,5")
    for q in (p, sub.choices["run"]):
        q.add_argument("--layout", default="blur", choices=list(LAYOUTS))
        q.add_argument("--highlight", default="#FFE500")
        q.add_argument("--no-whisper", action="store_true")
        q.add_argument("--no-tighten", action="store_true")
    a = ap.parse_args()
    cfg = load_config()
    if a.cmd in ("find", "run"):
        meta, words, workdir = get_transcript(a.source, a.lang)
        plan = find_clips(meta, words, workdir, a.clips, a.length, a.ai or cfg.get("provider", "gemini"), cfg, a.notes)
        print(plan_markdown(plan))
        if a.cmd == "run":
            render_plan(workdir / "clip_plan.json", layout=a.layout, highlight=a.highlight,
                        use_whisper=not a.no_whisper, tighten=not a.no_tighten)
    else:
        only = {int(x) for x in a.only.split(",") if x.strip()} or None
        render_plan(a.plan, only, layout=a.layout, highlight=a.highlight,
                    use_whisper=not a.no_whisper, tighten=not a.no_tighten)


if __name__ == "__main__":
    main()
