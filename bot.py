import os, json, subprocess, re, cloudinary, cloudinary.uploader, requests

LAST_IDS_FILE = "processed_ids.json"
COOKIES_FILE  = "/tmp/cookies.txt"
WEBHOOK_URL   = os.environ["WEBHOOK_URL"]

cloudinary.config(
    cloud_name = os.environ["CLOUDINARY_CLOUD_NAME"],
    api_key    = os.environ["CLOUDINARY_API_KEY"],
    api_secret = os.environ["CLOUDINARY_API_SECRET"],
)

def load_config():
    with open("config.json", "r", encoding="utf-8") as f:
        return json.load(f)

def load_processed_ids():
    try:
        with open(LAST_IDS_FILE, "r") as f: return json.load(f)
    except: return []

def save_processed_ids(ids):
    with open(LAST_IDS_FILE, "w") as f: json.dump(ids[-100:], f)

def clean_title(raw):
    """
    يحذف اسم الصفحة من العنوان مهما كانت لغته أو موضعه:
      "عنوان طويل | اسم_صفحة"         → "عنوان طويل"
      "اسم_صفحة | عنوان طويل"         → "عنوان طويل"
      "Fr title | تافرطة tafrata"      → "Fr title"
      "عنوان — VIF Media"              → "عنوان"
      "koooorama - عنوان"              → "عنوان"
      "VIF. عنوان"                     → "عنوان"
    المنطق: الجانب الأطول (عدد الكلمات) بعد | هو العنوان الحقيقي.
    """
    t = raw.strip()

    # 1) فاصل | → خذ الجانب الأطول كلمات
    m = re.match(r'^(.+?)\s*\|\s*(.+)$', t)
    if m:
        left  = m.group(1).strip()
        right = m.group(2).strip()
        lw = len(left.split())
        rw = len(right.split())
        if rw <= 3 and lw > rw:
            t = left       # الأيمن اسم صفحة قصير
        elif lw <= 3 and rw > lw:
            t = right      # الأيسر اسم صفحة قصير
        else:
            t = left       # الاثنان طويلان → خذ الأيسر (العنوان أولاً عادةً)

    # 2) فاصل — أو – → ما قبله هو العنوان
    t = re.split(r'\s*[—–]\s*\S.*$', t)[0].strip()

    # 3) كلمة لاتينية قصيرة في البداية + نقطة أو - → احذفها
    t = re.sub(r'^[a-zA-Z0-9._@-]{2,20}\s*[.\-:]\s+', '', t).strip()

    # 4) - كلمة_لاتينية_قصيرة في النهاية → احذفها
    t = re.sub(r'\s+-\s+[a-zA-Z0-9._]{2,20}\s*$', '', t).strip()

    # 5) تنظيف المسافات
    t = re.sub(r'\s+', ' ', t).strip()

    return t if len(t) >= 3 else raw

def get_font():
    """
    يعيد مسار أفضل خط عربي متاح.
    الأولوية: Montserrat-Arabic في المشروع ← DejaVu
    """
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Montserrat-Arabic-Bold.ttf")
    if os.path.exists(local):
        print(f"✅ Font: Montserrat-Arabic-Bold (local)")
        return local
    dv = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    print(f"⚠️ Using fallback font: DejaVu")
    return dv

def render_title_image(text, color_hex, video_w, video_h):
    from PIL import Image, ImageDraw, ImageFont

    hex_str   = color_hex.split("@")[0].replace("0x", "").replace("#", "")
    alpha_val = int(float(color_hex.split("@")[1]) * 255) if "@" in color_hex else 217
    bg_color  = (
        int(hex_str[0:2], 16),
        int(hex_str[2:4], 16),
        int(hex_str[4:6], 16),
        alpha_val
    )

    font_size = max(20, int(video_w * 0.0352))
    pad_h     = int(video_w * 0.05)
    pad_v     = int(video_h * 0.018)
    bar_w     = video_w - int(video_w * 0.08)
    usable    = bar_w - 2 * pad_h

    font_path = get_font()
    try:
        font = ImageFont.truetype(font_path, font_size)
    except:
        font = ImageFont.load_default()

    dummy = Image.new("RGBA", (1, 1))
    draw0 = ImageDraw.Draw(dummy)

    def get_tw(t):
        bb = draw0.textbbox((0, 0), t, font=font)
        return bb[2] - bb[0]

    words = text.split()
    lines, current = [], []
    for word in words:
        test = " ".join(current + [word])
        if get_tw(test) <= usable:
            current.append(word)
        else:
            if current:
                lines.append(" ".join(current))
            current = [word]
    if current:
        lines.append(" ".join(current))

    line_h = int(font_size * 1.5)
    bar_h  = len(lines) * line_h + 2 * pad_v

    img  = Image.new("RGBA", (bar_w, bar_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, bar_w - 1, bar_h - 1], fill=bg_color)

    for i, line in enumerate(lines):
        tw = get_tw(line)
        tx = (bar_w - tw) // 2
        ty = pad_v + i * line_h
        draw.text((tx + 2, ty + 2), line, font=font, fill=(0, 0, 0, 110))
        draw.text((tx,     ty),     line, font=font, fill=(255, 255, 255, 255))

    out_png = "/tmp/title_overlay.png"
    img.save(out_png, "PNG")

    bar_x = (video_w - bar_w) // 2
    bar_y = video_h - bar_h - int(video_h * 0.16)
    return out_png, bar_x, bar_y

def add_title_overlay(main, title, color, out, w, h):
    print("✍️ إضافة العنوان على الفيديو...")
    clean = clean_title(title)
    print(f"   العنوان الأصلي : {title[:70]}")
    print(f"   العنوان المنظّف: {clean}")

    try:
        png, bar_x, bar_y = render_title_image(clean, color, w, h)
    except Exception as e:
        print(f"⚠️ فشل رسم العنوان: {e}")
        import traceback; traceback.print_exc()
        subprocess.run(["cp", main, out])
        return os.path.exists(out)

    show_start   = 2.0
    fade_in_dur  = 0.8
    show_end     = 12.0
    fade_out_dur = 0.8
    fout_st      = show_end - fade_out_dur

    fc = (
        f"[1:v]format=yuva420p,"
        f"fade=t=in:st={show_start}:d={fade_in_dur}:alpha=1,"
        f"fade=t=out:st={fout_st}:d={fade_out_dur}:alpha=1[ovr];"
        f"[0:v][ovr]overlay=x={bar_x}:y={bar_y}[v]"
    )

    result = subprocess.run(
        ["ffmpeg", "-y",
         "-i", main,
         "-loop", "1", "-t", str(show_end + 1), "-i", png,
         "-filter_complex", fc,
         "-map", "[v]", "-map", "0:a",
         "-c:v", "libx264", "-c:a", "copy",
         "-preset", "fast", "-shortest", out],
        capture_output=True, text=True, timeout=600
    )

    if not os.path.exists(out):
        print("⚠️ fade overlay failed, trying enable-only fallback...")
        print(result.stderr[-600:])
        fc2 = (
            f"[1:v]format=yuva420p[ovr];"
            f"[0:v][ovr]overlay=x={bar_x}:y={bar_y}"
            f":enable='between(t,{show_start},{show_end})'[v]"
        )
        subprocess.run(
            ["ffmpeg", "-y",
             "-i", main,
             "-loop", "1", "-t", str(show_end + 1), "-i", png,
             "-filter_complex", fc2,
             "-map", "[v]", "-map", "0:a",
             "-c:v", "libx264", "-c:a", "copy",
             "-preset", "fast", "-shortest", out],
            capture_output=True, text=True, timeout=600
        )
    return os.path.exists(out)

def apply_green_screen(main, gs, out, w, h, dur):
    print("🎨 إضافة Green Screen...")
    r = subprocess.run(["ffmpeg","-y","-i",main,"-i",gs,"-filter_complex",
        f"[1:v]trim=duration={dur},scale={w}:{h},colorkey=0x00FF00:0.3:0.1,setpts=PTS-STARTPTS[g];[0:v][g]overlay=0:0[v]",
        "-map","[v]","-map","0:a","-c:v","libx264","-c:a","aac","-shortest","-preset","fast",out],
        capture_output=True, text=True, timeout=600)
    if not os.path.exists(out):
        subprocess.run(["ffmpeg","-y","-i",main,"-i",gs,"-filter_complex",
            f"[1:v]trim=duration={dur},scale={w}:{h},colorkey=0x00FF00:0.3:0.1,setpts=PTS-STARTPTS[g];[0:v][g]overlay=0:0[v]",
            "-map","[v]","-c:v","libx264","-shortest","-preset","fast",out],
            capture_output=True, text=True, timeout=600)
    return os.path.exists(out)

def add_outro(main, outro, out, w, h):
    print("🎬 إضافة Outro...")
    probe = subprocess.run(["ffprobe","-v","quiet","-print_format","json",
        "-show_streams","-show_format",outro], capture_output=True, text=True)
    has_audio, dur = False, 5
    try:
        info = json.loads(probe.stdout)
        has_audio = any(s["codec_type"]=="audio" for s in info["streams"])
        dur = float(info.get("format",{}).get("duration",5))
    except: pass
    if has_audio:
        fc = f"[0:v]scale={w}:{h},setsar=1[v0];[1:v]scale={w}:{h},setsar=1[v1];[v0][0:a][v1][1:a]concat=n=2:v=1:a=1[ov][oa]"
        maps = ["-map","[ov]","-map","[oa]"]
    else:
        fc = f"[0:v]scale={w}:{h},setsar=1[v0];[1:v]scale={w}:{h},setsar=1[v1];aevalsrc=0:d={dur}[sl];[v0][0:a][v1][sl]concat=n=2:v=1:a=1[ov][oa]"
        maps = ["-map","[ov]","-map","[oa]"]
    r = subprocess.run(["ffmpeg","-y","-i",main,"-i",outro,"-filter_complex",fc,
        *maps,"-c:v","libx264","-c:a","aac","-preset","fast",out],
        capture_output=True, text=True, timeout=600)
    if not os.path.exists(out):
        with open("/tmp/concat.txt","w") as f:
            f.write(f"file '{main}'\nfile '{outro}'\n")
        subprocess.run(["ffmpeg","-y","-f","concat","-safe","0","-i","/tmp/concat.txt",
            "-vf",f"scale={w}:{h},setsar=1","-c:v","libx264","-c:a","aac","-preset","fast",out],
            capture_output=True, text=True, timeout=600)
    return os.path.exists(out)

def process_for_publisher(main_video, publisher, w, h, dur, title):
    name = publisher["name"]
    print(f"🏭 معالجة لصفحة: {name}")
    gs_path    = f"/tmp/gs_{name}.mp4"
    out_path   = f"/tmp/out_{name}.mp4"
    titled_out = f"/tmp/titled_{name}.mp4"
    final      = f"/tmp/final_{name}.mp4"
    current    = main_video
    if download_from_cloudinary(publisher["green_screen_id"], gs_path):
        if apply_green_screen(current, gs_path, out_path, w, h, dur):
            current = out_path
    color = publisher.get("title_color", "0x1a237e@0.85")
    if add_title_overlay(current, title, color, titled_out, w, h):
        current = titled_out
    outro_path = f"/tmp/outro_{name}.mp4"
    if download_from_cloudinary(publisher["outro_id"], outro_path):
        if add_outro(current, outro_path, final, w, h):
            current = final
    return current

def upload_and_send(video_path, title, publisher_name):
    safe = re.sub(r"[^a-z0-9]", "_", publisher_name.lower())
    result = cloudinary.uploader.upload(video_path,
        resource_type="video", public_id=f"final_{safe}", overwrite=True)
    requests.post(WEBHOOK_URL, json={
        "video_url": result["secure_url"],
        "title": title,
        "publisher": publisher_name
    }, timeout=30)
    print(f"✅ تم إرسال {publisher_name}")

def download_from_cloudinary(public_id, out):
    url = f"https://res.cloudinary.com/{os.environ['CLOUDINARY_CLOUD_NAME']}/video/upload/{public_id}.mp4"
    subprocess.run(["wget","-q","-O",out,url], timeout=60)
    return os.path.exists(out)

def download_video(url):
    subprocess.run(["yt-dlp","--cookies",COOKIES_FILE,"-o","/tmp/main.mp4",
        "--format","best[ext=mp4]/best","--no-warnings",url], timeout=300)
    return os.path.exists("/tmp/main.mp4")

def get_video_info(path):
    probe = subprocess.run(["ffprobe","-v","quiet","-print_format","json",
        "-show_streams","-show_format",path], capture_output=True, text=True)
    try:
        info = json.loads(probe.stdout)
        vs = next((s for s in info["streams"] if s["codec_type"]=="video"), None)
        return vs["width"], vs["height"], float(info["format"].get("duration",60))
    except: return 1080, 1920, 60

def get_videos_from_source(source):
    print(f"🔍 جلب من: {source['name']}")
    script = f"""
import json, time, re
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
options = Options()
options.add_argument("--headless")
options.add_argument("--no-sandbox")
options.add_argument("--disable-dev-shm-usage")
options.add_argument("--disable-gpu")
options.add_argument("--window-size=1920,1080")
service = Service(ChromeDriverManager().install())
driver = webdriver.Chrome(service=service, options=options)
driver.get("https://www.facebook.com")
time.sleep(2)
cookies = []
try:
    with open("/tmp/cookies.txt", "r") as f:
        for line in f:
            if line.startswith("#") or not line.strip(): continue
            parts = line.strip().split("\\t")
            if len(parts) >= 7:
                cookies.append({{"name": parts[5], "value": parts[6], "domain": parts[0]}})
except: pass
for c in cookies:
    try: driver.add_cookie(c)
    except: pass
driver.get("{source['url']}")
time.sleep(8)
driver.execute_script("window.scrollTo(0, 0);")
time.sleep(2)
driver.execute_script("window.scrollTo(0, 2000);")
time.sleep(2)
driver.execute_script("window.scrollTo(0, 0);")
time.sleep(1)
src = driver.page_source
driver.quit()
links = re.findall(r'href="(/reel/([0-9]+)/[^"]*?)"', src)
videos = []
seen = set()
for path, vid_id in links:
    if vid_id not in seen:
        seen.add(vid_id)
        videos.append({{"id": vid_id, "url": "https://www.facebook.com/reel/" + vid_id + "/"}})
print(json.dumps(videos[:10]))
"""
    with open("/tmp/sel.py", "w") as f: f.write(script)
    result = subprocess.run(["python", "/tmp/sel.py"], capture_output=True, text=True, timeout=90)
    videos = []
    for line in reversed(result.stdout.strip().split("\n")):
        if line.startswith("["):
            videos = json.loads(line)
            break
    for v in videos:
        try:
            t = subprocess.run(["yt-dlp","--get-title","--no-warnings",
                "--cookies", COOKIES_FILE, v["url"]],
                capture_output=True, text=True, timeout=30)
            v["title"] = t.stdout.strip() or "بدون عنوان"
        except: v["title"] = "بدون عنوان"
        v["source"] = source["name"]
    print(f"  ✅ {len(videos)} فيديو من {source['name']}")
    return videos

def scale_to_target(src, out, target_w=1080, target_h=1920):
    print(f"📐 تحجيم الفيديو إلى {target_w}×{target_h}...")
    vf = (
        f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
        f"crop={target_w}:{target_h},"
        f"setsar=1"
    )
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-vf", vf,
         "-c:v", "libx264", "-c:a", "aac", "-preset", "fast", out],
        capture_output=True, text=True, timeout=600
    )
    if os.path.exists(out):
        print(f"  ✅ تم التحجيم بنجاح")
        return True
    print(f"  ❌ فشل التحجيم: {r.stderr[-300:]}")
    return False

def cleanup(names):
    for name in names:
        for f in [f"/tmp/gs_{name}.mp4", f"/tmp/out_{name}.mp4",
                  f"/tmp/titled_{name}.mp4", f"/tmp/final_{name}.mp4",
                  f"/tmp/outro_{name}.mp4"]:
            if os.path.exists(f): os.remove(f)
    for f in ["/tmp/main.mp4", "/tmp/main_scaled.mp4",
              "/tmp/concat.txt", "/tmp/sel.py", "/tmp/title_overlay.png"]:
        if os.path.exists(f): os.remove(f)

# ── التنفيذ الرئيسي ──────────────────────────────────────────
TARGET_W = 1080
TARGET_H = 1920

print("🤖 بدء تشغيل البوت...")
config = load_config()
processed_ids = load_processed_ids()
all_videos = []
for source in config["sources"]:
    try: all_videos.extend(get_videos_from_source(source))
    except Exception as e: print(f"❌ خطأ في {source['name']}: {e}")

new_video = next((v for v in all_videos if v["id"] not in processed_ids), None)

if not new_video:
    print("ℹ️ لا يوجد فيديو جديد")
else:
    print(f"🆕 {new_video['title'][:60]}")
    if not download_video(new_video["url"]): exit(1)

    src_w, src_h, dur = get_video_info("/tmp/main.mp4")
    print(f"  📏 المقاس الأصلي: {src_w}×{src_h}")

    if src_w == TARGET_W and src_h == TARGET_H:
        main_ready = "/tmp/main.mp4"
        print(f"  ✅ المقاس مطابق، لا حاجة لتحجيم")
    else:
        main_ready = "/tmp/main_scaled.mp4"
        if not scale_to_target("/tmp/main.mp4", main_ready, TARGET_W, TARGET_H):
            main_ready = "/tmp/main.mp4"

    w, h = TARGET_W, TARGET_H
    names = []
    for pub in config["publishers"]:
        try:
            final = process_for_publisher(main_ready, pub, w, h, dur, new_video["title"])
            upload_and_send(final, new_video["title"], pub["name"])
            names.append(pub["name"])
        except Exception as e: print(f"❌ {pub['name']}: {e}")
    processed_ids.append(new_video["id"])
    save_processed_ids(processed_ids)
    cleanup(names)
    print("🎉 اكتمل بنجاح!")
