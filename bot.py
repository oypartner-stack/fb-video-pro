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
    """
    يأخذ العنوان الخام ويحذف اسم الصفحة — عادةً يكون بعد | أو - في النهاية
    مثال: "خبر مهم | koooorama" → "خبر مهم"
    """
    # احذف كل ما بعد | أو — أو - إذا جاء في النهاية
    title = re.split(r'\s*[\|—–-]\s*[^|—–-]*$', raw_title)[0].strip()
    # نظّف الرموز التي تكسر ffmpeg
    title = re.sub(r"[':,\\\[\]()]", " ", title)
    title = re.sub(r"\s+", " ", title).strip()
    return title or raw_title

def download_cairo_font():
    """تحميل خط Cairo العربي من Google Fonts"""
    font_path = "/tmp/Cairo-Bold.ttf"
    if os.path.exists(font_path):
        return font_path
    try:
        url = "https://github.com/google/fonts/raw/main/ofl/cairo/Cairo%5Bslnt%2Cwght%5D.ttf"
        r = subprocess.run(["wget", "-q", "-O", font_path, url], timeout=30)
        if os.path.exists(font_path) and os.path.getsize(font_path) > 10000:
            return font_path
    except: pass
    # fallback: خط DejaVu
    return "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

def split_title_lines(title, max_chars_per_line):
    """
    يقسّم العنوان على سطرين حسب عدد الحروف الفعلي — لا يتجاوز عرض الشريط
    """
    if len(title) <= max_chars_per_line:
        return [title]

    words = title.split()
    lines = []
    current = ""
    for word in words:
        test = (current + " " + word).strip()
        if len(test) <= max_chars_per_line:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
            if len(lines) == 1:  # نكتفي بسطرين فقط
                # باقي الكلمات في السطر الثاني مهما كان
                remaining = " ".join(words[words.index(word):])
                # قطّع إذا طال جداً
                lines.append(remaining[:max_chars_per_line])
                current = ""
                break
    if current:
        lines.append(current)
    return lines[:2]  # أقصاه سطران

def add_title_overlay(main, title, color, out, w, h):
    print("✍️ إضافة العنوان على الفيديو...")

    # ── تنظيف العنوان وحذف اسم الصفحة ──────────────────────────
    clean = clean_title(title)

    # ── تحميل خط Cairo ───────────────────────────────────────────
    font = download_cairo_font()

    # ── الأبعاد ──────────────────────────────────────────────────
    font_size     = int(h * 0.038)          # حجم الخط
    line_spacing  = int(font_size * 1.5)    # مسافة بين السطرين
    pad_v         = int(h * 0.022)          # حشوة عمودية
    bar_w         = w - int(w * 0.08)       # عرض الشريط 92% من الفيديو
    bar_x         = (w - bar_w) // 2        # توسيط أفقي

    # ── تقدير عدد الحروف في السطر بناءً على عرض الشريط والخط ────
    # تقدير تجريبي: حرف عربي ≈ font_size * 0.6 عرضاً
    max_chars = int(bar_w / (font_size * 0.62))

    # ── تقسيم السطور ─────────────────────────────────────────────
    lines = split_title_lines(clean, max_chars)
    num_lines = len(lines)

    bar_h  = num_lines * line_spacing + 2 * pad_v
    # ── موضع الشريط: أسفل الفيديو بهامش 16% ─────────────────────
    bar_y  = h - bar_h - int(h * 0.16)

    # ── مواضع النص ───────────────────────────────────────────────
    text_y1 = bar_y + pad_v
    text_y2 = text_y1 + line_spacing

    # ── تأثيرات الظهور والاختفاء (fade in 0.6s — fade out 0.6s) ──
    # الشريط يظهر عند t=0 ويختفي عند t=10
    # alpha = fade_in * fade_out
    fade_dur   = 0.6   # مدة التأثير بالثواني
    show_end   = 10.0  # وقت الاختفاء

    # alpha expression للـ drawbox (يدعم alpha في اللون مباشرة)
    # نستخدم overlay شفاف بدل drawbox لدعم الـ fade
    # الحل: نبني الشريط كـ overlay منفصل مع alphamerge

    # ── filter_complex بتأثير fade باستخدام format+colorchannelmixer ─
    # نرسم الشريط على صورة سوداء ثم نعمل overlay مع alpha متحرك

    alpha_expr = (
        f"if(lt(t,{fade_dur}),t/{fade_dur},"          # fade in
        f"if(gt(t,{show_end - fade_dur}),"
        f"({show_end}-t)/{fade_dur},1))"              # fade out
    )
    # نضع 0 خارج النافذة [0, show_end]
    alpha_full = f"if(between(t,0,{show_end}),{alpha_expr},0)"

    # لون الشريط بدون alpha (سنتحكم في alpha بشكل منفصل)
    color_solid = color.split("@")[0]  # مثلاً 0x1a237e

    fc_parts = []

    # 1) صورة ملونة بحجم الشريط
    fc_parts.append(
        f"color=c={color_solid}:s={bar_w}x{bar_h}[bar_base]"
    )

    # 2) كتابة النص على الشريط
    txt1 = lines[0].replace("'", "\\'")
    fc_parts.append(
        f"[bar_base]drawtext=text='{txt1}'"
        f":fontfile={font}:fontsize={font_size}"
        f":fontcolor=white:x=(w-text_w)/2:y={pad_v}"
        + (
            f",drawtext=text='{lines[1].replace(chr(39), chr(92)+chr(39))}'"
            f":fontfile={font}:fontsize={font_size}"
            f":fontcolor=white:x=(w-text_w)/2:y={pad_v + line_spacing}"
            if num_lines == 2 else ""
        )
        + "[bar_txt]"
    )

    # 3) ضع الشريط على الفيديو مع alpha متحرك
    fc_parts.append(
        f"[bar_txt]format=yuva420p[bar_rgba]"
    )
    fc_parts.append(
        f"[bar_rgba]colorchannelmixer=aa={alpha_full}[bar_alpha]"
    )
    fc_parts.append(
        f"[0:v][bar_alpha]overlay=x={bar_x}:y={bar_y}[v]"
    )

    fc = ";".join(fc_parts)

    result = subprocess.run(
        ["ffmpeg", "-y", "-i", main, "-filter_complex", fc,
         "-map", "[v]", "-map", "0:a",
         "-c:v", "libx264", "-c:a", "copy", "-preset", "fast", out],
        capture_output=True, text=True, timeout=600
    )

    if not os.path.exists(out):
        print(f"⚠️ ffmpeg error: {result.stderr[-500:]}")
        # fallback بسيط بدون fade
        fc_simple = (
            f"[0:v]drawbox=x={bar_x}:y={bar_y}:w={bar_w}:h={bar_h}"
            f":color={color}:t=fill:enable='between(t,0,{show_end})'"
            f",drawtext=text='{lines[0].replace(chr(39),chr(32))}'"
            f":fontfile={font}:fontsize={font_size}:fontcolor=white"
            f":x=(w-text_w)/2:y={text_y1}:enable='between(t,0,{show_end})'"
        )
        if num_lines == 2:
            fc_simple += (
                f",drawtext=text='{lines[1].replace(chr(39),chr(32))}'"
                f":fontfile={font}:fontsize={font_size}:fontcolor=white"
                f":x=(w-text_w)/2:y={text_y2}:enable='between(t,0,{show_end})'"
            )
        fc_simple += "[v]"
        subprocess.run(
            ["ffmpeg", "-y", "-i", main, "-filter_complex", fc_simple,
             "-map", "[v]", "-map", "0:a",
             "-c:v", "libx264", "-c:a", "copy", "-preset", "fast", out],
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
              "/tmp/Cairo-Bold.ttf"]:
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
