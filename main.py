import os, json, random, re, subprocess, textwrap, asyncio, pathlib, shutil
import requests, edge_tts
import yt_dlp
import urllib.parse
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

ROOT = pathlib.Path(__file__).parent
STATE = ROOT / "state" / "used.json"
QUEUE = ROOT / "inbox" / "queue.json"
WORK = ROOT / "work"

PEXELS_KEY = os.environ.get("PEXELS_API_KEY", "")
GEMINI_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
LANGUAGE = os.environ.get("CHANNEL_LANGUAGE", "English")
VOICE = os.environ.get("TTS_VOICE", "en-US-AriaNeural")
PRIVACY = os.environ.get("PRIVACY", "public")
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
FONT = os.environ.get("FONT_FILE", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")

TOPICS = [
    "cooking food", "shawarma", "pizza", "street food", "kebab grill",
    "fresh fruits", "fresh vegetables", "amazing food", "baking dessert",
    "sweet dessert", "ice cream", "chef kitchen", "fruit cutting",
]
YT_QUERIES = [
    "shawarma", "pizza making", "street food", "cooking recipe", "kebab",
    "fruit cutting", "vegetables recipe", "amazing food", "dessert recipe",
    "ice cream making", "satisfying food",
]


def load_used():
    return json.loads(STATE.read_text()) if STATE.exists() else []


def save_used(used):
    STATE.write_text(json.dumps(used, indent=1))


def download(url, dest):
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)


def pick_inbox(used):
    """Clips the channel owner gave written permission for."""
    if not QUEUE.exists():
        return None
    for item in json.loads(QUEUE.read_text()):
        key = "inbox:" + item["file"]
        path = ROOT / item["file"]
        if key not in used and path.exists():
            return key, path, item["topic"], item["credit"]
    return None


def pick_youtube(used):
    """SOURCE_CHANNELS set -> only those channels (owner permission).
    Not set -> general search across YouTube, Creative Commons videos only."""
    channels = [u.strip() for u in os.environ.get("SOURCE_CHANNELS", "").split(",") if u.strip()]
    base = {"quiet": True, "no_warnings": True}
    if os.environ.get("YT_COOKIES"):
        cf = WORK / "cookies.txt"
        cf.write_text(os.environ["YT_COOKIES"])
        base["cookiefile"] = str(cf)
    if channels:
        random.shuffle(channels)
        sources = [(c.rstrip("/") + "/shorts", False) for c in channels]
    else:
        qs = YT_QUERIES[:]
        random.shuffle(qs)
        sources = [("https://www.youtube.com/results?search_query="
                    + urllib.parse.quote(q + " shorts") + "&sp=EgIwAQ%253D%253D", True) for q in qs[:4]]
    for src, need_cc in sources:
        try:
            with yt_dlp.YoutubeDL({**base, "extract_flat": True, "skip_download": True}) as y:
                info = y.extract_info(src, download=False)
        except Exception as ex:
            print("search failed:", ex)
            continue
        entries = [e for e in (info.get("entries") or [])
                   if e and e.get("id") and len(e["id"]) == 11 and f"yt:{e['id']}" not in used]
        random.shuffle(entries)
        for e in entries[:6]:
            url = "https://www.youtube.com/watch?v=" + e["id"]
            try:
                with yt_dlp.YoutubeDL({**base, "skip_download": True}) as y:
                    meta = y.extract_info(url, download=False)
                lic = (meta.get("license") or "").lower()
                if need_cc and "creative commons" not in lic:
                    continue
                if not (0 < (meta.get("duration") or 0) <= 180) or (meta.get("height") or 0) <= (meta.get("width") or 0):
                    continue
                opts = {**base, "format": "bv[height<=1080]/b[height<=1080]",
                        "outtmpl": str(WORK / "clip.%(ext)s")}
                with yt_dlp.YoutubeDL(opts) as y:
                    meta = y.extract_info(url, download=True)
                    path = pathlib.Path(y.prepare_filename(meta))
            except Exception as ex:
                print("skip", e["id"], ex)
                continue
            who = meta.get("uploader") or "unknown"
            if need_cc:
                credit = f"Video by {who} ({url}), licensed under Creative Commons Attribution (CC BY)"
            else:
                credit = f"Video by {who} ({url}), used with the channel owner's written permission"
            return f"yt:{e['id']}", path, meta.get("title", "food video"), credit
    return None


def pick_pexels(used):
    for _ in range(8):
        topic = random.choice(TOPICS)
        r = requests.get(
            "https://api.pexels.com/videos/search",
            headers={"Authorization": PEXELS_KEY},
            params={"query": topic, "orientation": "portrait", "size": "medium",
                    "per_page": 30, "page": random.randint(1, 3)},
            timeout=30,
        )
        r.raise_for_status()
        vids = [v for v in r.json().get("videos", [])
                if f"pexels:{v['id']}" not in used and 8 <= v["duration"] <= 60]
        random.shuffle(vids)
        for v in vids:
            files = [f for f in v["video_files"]
                     if f.get("width") and f.get("height")
                     and f["height"] > f["width"] and f["width"] >= 720]
            if not files:
                continue
            files.sort(key=lambda f: f["width"])
            big = [f for f in files if f["width"] >= 1080]
            chosen = big[0] if big else files[-1]
            slug = v["url"].rstrip("/").split("/")[-1]
            slug = re.sub(r"-\d+$", "", slug).replace("-", " ")
            hint = f"{slug} ({topic})"
            dest = WORK / "clip.mp4"
            download(chosen["link"], dest)
            credit = f"Video by {v['user']['name']} on Pexels: {v['url']}"
            return f"pexels:{v['id']}", dest, hint, credit
    raise RuntimeError("No Pexels video found")


def write_script(hint, seconds):
    seconds = int(max(25, min(seconds, 165)))
    words = int(seconds * 2.3)
    prompt = f"""You are a top-tier YouTube Shorts scriptwriter for a viral food channel (cooking, street food, shawarma, pizza, desserts, ice cream, fruits, vegetables).
The footage on screen: {hint}.
Language: {LANGUAGE}.
Write a voiceover of about {words} words. Spoken at a natural pace it must last roughly {seconds} seconds.
Style rules:
- The first sentence is a scroll-stopping hook: a surprising fact, a bold question, or a curiosity gap. Never start with greetings, "welcome", "today we", or "in this video".
- Short, punchy, spoken sentences. Vary the rhythm. Talk directly to the viewer using "you".
- Use sensory words (sizzle, crunch, steam, aroma) tied to what is plausibly visible.
- Plant two open loops (for example "but the real secret comes later") and pay them off before the end.
- Include two or three genuinely interesting, TRUE, general facts about the food, its origin, or the technique. Do NOT invent claims about this specific video, people, places or brands, and make no medical or health promises.
- End with a line that loops back to the opening hook so the video replays naturally, then one very short call to follow.
- Plain text only: no emojis, hashtags, stage directions or labels.
Return ONLY JSON with keys:
- title: max 60 characters, curiosity-driven, no false clickbait
- narration: the voiceover text
- description: 2 short sentences
- tags: list of 8 short keywords"""
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
        params={"key": GEMINI_KEY},
        json={"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": {"responseMimeType": "application/json"}},
        timeout=120,
    )
    r.raise_for_status()
    text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)


def tts(text, out):
    asyncio.run(edge_tts.Communicate(text, VOICE).save(str(out)))


def duration(path):
    o = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)], text=True)
    return float(o.strip())


def build_video(clip, audio, title, out):
    dur = duration(audio) + 0.6
    tf = WORK / "title.txt"
    tf.write_text(textwrap.fill(title, 20))
    fc = ("[0:v]split[a][b];"
          "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=25:5[bg];"
          "[b]scale=1080:-2:force_original_aspect_ratio=decrease[fg];"
          "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,"
          f"drawtext=fontfile={FONT}:textfile={tf}:fontsize=64:fontcolor=white:"
          "borderw=4:bordercolor=black:x=(w-text_w)/2:y=220:enable='lt(t,4)'[v]")
    subprocess.check_call([
        "ffmpeg", "-y", "-loglevel", "error",
        "-stream_loop", "-1", "-i", str(clip), "-i", str(audio),
        "-t", f"{dur:.2f}", "-filter_complex", fc,
        "-map", "[v]", "-map", "1:a:0", "-r", "30",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", str(out)])


def upload(path, meta, credit):
    creds = Credentials(
        None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/youtube"],
    )
    yt = build("youtube", "v3", credentials=creds)
    tags = [str(t) for t in meta.get("tags", [])][:10]
    desc = (meta["description"] + "\n\n" + credit + "\nNarration: AI voice.\n\n#Shorts "
            + " ".join("#" + t.replace(" ", "") for t in tags[:3]))
    body = {
        "snippet": {"title": meta["title"][:100], "description": desc,
                    "tags": tags, "categoryId": "26"},
        "status": {"privacyStatus": PRIVACY, "selfDeclaredMadeForKids": False},
    }
    req = yt.videos().insert(
        part="snippet,status", body=body,
        media_body=MediaFileUpload(str(path), mimetype="video/mp4", resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return resp["id"]


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir()
    used = load_used()
    picked = pick_inbox(used) or pick_youtube(used) or (pick_pexels(used) if PEXELS_KEY else None)
    if not picked:
        raise SystemExit("No video found. Check SOURCE_CHANNELS, cookies, or inbox.")
    key, clip, hint, credit = picked
    print("Source:", key, "|", hint)
    meta = write_script(hint, duration(clip))
    print("Title:", meta["title"])
    audio = WORK / "voice.mp3"
    tts(meta["narration"], audio)
    out = WORK / "final.mp4"
    build_video(clip, audio, meta["title"], out)
    if DRY_RUN:
        print("DRY_RUN: built", out, "- not uploading")
        return
    vid = upload(out, meta, credit)
    print("Uploaded: https://youtube.com/shorts/" + vid)
    used.append(key)
    save_used(used)


if __name__ == "__main__":
    main()
