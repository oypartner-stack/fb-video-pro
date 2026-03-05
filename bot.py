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

def clean_title(raw_title):
    """يحذف اسم الصفحة من العنوان — عادةً يكون بعد | أو - في النهاية"""
    title = re.split(r'\s*[\|—–]\s*[^|—–]*$', raw_title)[0].strip()
    title = re.sub(r"\s+", " ", title).strip()
    return title or raw_title

def ensure_deps():
    """تثبيت المكتبات المطلوبة لرسم النص العربي"""
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        subprocess.run(
            ["pip", "install", "arabic-reshaper", "python-bidi", "Pillow", "--quiet"],
            timeout=60
        )

def download_cairo_font():
    """تحميل خط Cairo Bold"""
    font_path = "/tmp/CairoBold.ttf"
    if os.path.exists(font_path) and os.path.getsize(font_path) > 50000:
        return font_path
    urls = [
        "https://github.com/google/fonts/raw/main/ofl/cairo/static/Cairo-Bold.ttf",
        "https://fonts.gstatic.com/s/cairo/v28/SLXgc1nY6HkvalIvTp0qWg.ttf",
    ]
    for url in urls:
        try:
            subprocess.run(["wget", "-q", "-O", font_path, url], timeout=30)
            if os.path.exists(font_path) and os.path.getsize(font_path) > 50000:
                print("✅ Cairo font downloaded")
                return font_path
        except: pass
    for p in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]:
        if os.path.exists(p): return p
    return None

def render_title_image(text, color_hex, video_w, video_h, font_path):
    """
    يرسم PNG شفاف يحتوي الشريط الملون والعنوان العربي الصحيح بخط BOLD
    """
    import arabic_reshaper
    from bidi.algorithm import get_display
    from PIL import Image, ImageDraw, ImageFont

    # ── تحويل اللون ───────────────────────────────────────────
    hex_color = color_hex.split("@")[0].replace("0x", "").replace("#", "")
    alpha_val = int(float(color_hex.split("@")[1]) * 255) if "@" in color_hex else 217
    r_c = int(hex_color[0:2], 16)
    g_c = int(hex_color[2:4], 16)
    b_c = int(hex_color[4:6], 16)
    bg_color = (r_c, g_c, b_c, alpha_val)

    # ── أبعاد الشريط ──────────────────────────────────────────
    pad_h     = int(video_w * 0.05)
    pad_v     = int(video_h * 0.022)
    bar_w     = video_w - int(video_w * 0.08)
    font_size = int(video_h * 0.044)
    usable_w  = bar_w - 2 * pad_h

    # ── تحميل الخط ────────────────────────────────────────────
    try:
        font = ImageFont.truetype(font_path, font_size)
    except:
        font = ImageFont.load_default()

    def get_text_width(t):
        dummy_img = Image.new("RGBA", (1, 1))
        d = ImageDraw.Draw(dummy_img)
        bbox = d.textbbox((0, 0), t, font=font)
        return bbox[2] - bbox[0]

    def to_bidi(t):
        return get_display(arabic_reshaper.reshape(t))

    # ── تقسيم النص إلى سطرين بناءً على عرض حقيقي ────────────
    words = text.split()
    lines_raw = []
    current_words = []

    for word in words:
        test_words = current_words + [word]
        test_str = " ".join(test_words)
        if get_text_width(to_bidi(test_str)) <= usable_w:
            current_words = test_words
        else:
            if current_words:
                lines_raw.append(" ".join(current_words))
            current_words = [word]
            # إذا وصلنا للسطر الثاني، نضع الباقي كله فيه
            if len(lines_raw) >= 1:
                remaining = " ".join([word] + words[words.index(word)+1:])
                # قطّع إذا تجاوز العرض
                if get_text_width(to_bidi(remaining)) > usable_w:
                    # قطّع بالحروف
                    cut = ""
                    for ch in remaining:
                        if get_text_width(to_bidi(cut + ch)) <= usable_w:
                            cut += ch
                        else:
                            break
                    remaining = cut
                lines_raw.append(remaining)
                current_words = []
                break

    if current_words:
        lines_raw.append(" ".join(current_words))

    lines_raw = lines_raw[:2]
    lines_bidi = [to_bidi(l) for l in lines_raw]

    # ── حساب ارتفاع الشريط ────────────────────────────────────
    line_h = int(font_size * 1.55)
    bar_h  = len(lines_bidi) * line_h + 2 * pad_v

    # ── رسم الصورة ────────────────────────────────────────────
    img  = Image.new("RGBA", (bar_w, bar_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, bar_w - 1, bar_h - 1], fill=bg_color)

    for i, line in enumerate(lines_bidi):
        bbox = draw.textbbox((0, 0), line, font=font)
        tw = bbox[2] - bbox[0]
        tx = (bar_w - tw) // 2
        ty = pad_v + i * line_h
        # ظل خفيف للنص
        draw.text((tx + 2, ty + 2), line, font=font, fill=(0, 0, 0, 120))
        # النص الأبيض
        draw.text((tx, ty), line, font=font, fill=(255, 255, 255, 255))

    out_png = "/tmp/title_overlay.png"
    img.save(out_png, "PNG")

    bar_x = (video_w - bar_w) // 2
    bar_y = video_h - bar_h - int(video_h * 0.16)

    return out_png, bar_x, bar_y, bar_h

def add_title_overlay(main, title, color, out, w, h):
    print("✍️ إضافة العنوان على الفيديو...")

    ensure_deps()
    clean = clean_title(title)
    font_path = download_cairo_font()

    try:
        png, bar_x, bar_y, bar_h = render_title_image(clean, color, w, h, font_path)
    except Exception as e:
        print(f"⚠️ فشل رسم العنوان: {e}")
        import traceback; traceback.print_exc()
        subprocess.run(["cp", main, out])
        return os.path.exists(out)

    show_end = 10.0
    fade_dur = 0.6

    # ── fade in / fade out ────────────────────────────────────
    alpha_expr = (
        f"if(lt(t,{fade_dur}),t/{fade_dur},"
        f"if(gt(t,{show_end - fade_dur}),({show_end}-t)/{fade_dur},1))"
    )
    alpha_full = f"if(between(t,0,{show_end}),{alpha_expr},0)"

    fc = (
        f"[1:v]format=yuva420p,"
        f"colorchannelmixer=aa='{alpha_full}'[ovr];"
        f"[0:v][ovr]overlay=x={bar_x}:y={bar_y}[v]"
    )

    result = subprocess.run(
        ["ffmpeg", "-y",
         "-i", main,
         "-loop", "1", "-i", png,
         "-filter_complex", fc,
         "-map", "[v]", "-map", "0:a",
         "-c:v", "libx264", "-c:a", "copy",
         "-preset", "fast", "-shortest", out],
        capture_output=True, text=True, timeout=600
    )

    if not os.path.exists(out):
        print(f"⚠️ ffmpeg fade error, trying simple overlay...")
        print(result.stderr[-600:])
        # fallback بدون fade
        fc2 = (
            f"[1:v]format=yuva420p[ovr];"
            f"[0:v][ovr]overlay=x={bar_x}:y={bar_y}:enable='between(t,0,{show_end})'[v]"
        )
        subprocess.run(
            ["ffmpeg", "-y",
             "-i", main,
             "-loop", "1", "-i", png,
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
        capture_output=True,text=True,timeout=600)
    if not os.path.exists(out):
        subprocess.run(["ffmpeg","-y","-i",main,"-i",gs,"-filter_complex",
            f"[1:v]trim=duration={dur},scale={w}:{h},colorkey=0x00FF00:0.3:0.1,setpts=PTS-STARTPTS[g];[0:v][g]overlay=0:0[v]",
            "-map","[v]","-c:v","libx264","-shortest","-preset","fast",out],
            capture_output=True,text=True,timeout=600)
    return os.path.exists(out)

def add_outro(main, outro, out, w, h):
    print("🎬 إضافة Outro...")
    probe = subprocess.run(["ffprobe","-v","quiet","-print_format","json",
        "-show_streams","-show_format",outro],capture_output=True,text=True)
    has_audio,dur = False,5
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
        capture_output=True,text=True,timeout=600)
    if not os.path.exists(out):
        with open("/tmp/concat.txt","w") as f:
            f.write(f"file '{main}'\nfile '{outro}'\n")
        subprocess.run(["ffmpeg","-y","-f","concat","-safe","0","-i","/tmp/concat.txt",
            "-vf",f"scale={w}:{h},setsar=1","-c:v","libx264","-c:a","aac","-preset","fast",out],
            capture_output=True,text=True,timeout=600)
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
    subprocess.run(["wget","-q","-O",out,url],timeout=60)
    return os.path.exists(out)

def download_video(url):
    subprocess.run(["yt-dlp","--cookies",COOKIES_FILE,"-o","/tmp/main.mp4",
        "--format","best[ext=mp4]/best","--no-warnings",url],timeout=300)
    return os.path.exists("/tmp/main.mp4")

def get_video_info(path):
    probe = subprocess.run(["ffprobe","-v","quiet","-print_format","json",
        "-show_streams","-show_format",path], capture_output=True, text=True)
    try:
        info = json.loads(probe.stdout)
        vs = next((s for s in info["streams"] if s["codec_type"]=="video"),None)
        return vs["width"],vs["height"],float(info["format"].get("duration",60))
    except: return 1080,1920,60

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

def cleanup(names):
    for name in names:
        for f in [f"/tmp/gs_{name}.mp4", f"/tmp/out_{name}.mp4",
                  f"/tmp/titled_{name}.mp4", f"/tmp/final_{name}.mp4",
                  f"/tmp/outro_{name}.mp4"]:
            if os.path.exists(f): os.remove(f)
    for f in ["/tmp/main.mp4", "/tmp/concat.txt", "/tmp/sel.py",
              "/tmp/CairoBold.ttf", "/tmp/title_overlay.png"]:
        if os.path.exists(f): os.remove(f)

# ── التنفيذ الرئيسي ──────────────────────────────────────
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
    w, h, dur = get_video_info("/tmp/main.mp4")
    names = []
    for pub in config["publishers"]:
        try:
            final = process_for_publisher("/tmp/main.mp4", pub, w, h, dur, new_video["title"])
            upload_and_send(final, new_video["title"], pub["name"])
            names.append(pub["name"])
        except Exception as e: print(f"❌ {pub['name']}: {e}")
    processed_ids.append(new_video["id"])
    save_processed_ids(processed_ids)
    cleanup(names)
    print("🎉 اكتمل بنجاح!")
