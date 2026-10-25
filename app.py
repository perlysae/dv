import json, os, subprocess, threading, uuid
from fractions import Fraction
from flask import Flask, jsonify, request, send_file, abort

BASE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(BASE, "work")
os.makedirs(WORK, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 ** 3  # 4 GB
jobs = {}


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} gagal:\n{p.stderr[-800:]}")
    return p.stdout


def probe(path):
    out = run(["ffprobe", "-v", "error", "-count_packets", "-select_streams", "v:0",
               "-show_entries", "stream=r_frame_rate,nb_read_packets,width,height", "-of", "json", path])
    s = json.loads(out)["streams"][0]
    fps = Fraction(s["r_frame_rate"])
    frames = int(s["nb_read_packets"])
    a = run(["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", path]).strip()
    return float(fps), frames, bool(a), int(s["width"]), int(s["height"])


def set_job(jid, **kw):
    jobs[jid].update(kw)


import math, re

def hlg_to_pq(code10):
    """Perkiraan: kode HLG 10-bit (limited) -> nilai PQ 12-bit (skala 0-4095), puncak tampilan 1000 nit."""
    e = min(max((code10 - 64) / 876.0, 0.0), 1.0)
    a, b, c = 0.17883277, 0.28466892, 0.55991073
    sc = (e * e) / 3 if e <= 0.5 else (math.exp((e - c) / a) + b) / 12
    nits = 1000.0 * (min(sc, 1.0) ** 1.2)
    y = nits / 10000.0
    m1, m2, c1, c2, c3 = 0.1593017578125, 78.84375, 0.8359375, 18.8515625, 18.6875
    pq = ((c1 + c2 * y ** m1) / (1 + c3 * y ** m1)) ** m2
    return int(round(min(max(pq, 0), 1) * 4095))


def analyze_shots(hevc, stats_path):
    """Ukur kecerahan tiap frame, bagi jadi shot, hitung L1 (min/max/avg PQ) per shot."""
    run(["ffmpeg", "-y", "-i", hevc, "-vf",
         f"scale=480:-2,signalstats,metadata=print:file={stats_path}", "-an", "-f", "null", "-"])
    mins, maxs, avgs = [], [], []
    cur = {}
    for line in open(stats_path):
        m = re.match(r"lavfi\.signalstats\.(YMIN|YMAX|YAVG)=([0-9.]+)", line.strip())
        if m:
            cur[m.group(1)] = float(m.group(2))
            if len(cur) == 3:
                mins.append(cur["YMIN"]); maxs.append(cur["YMAX"]); avgs.append(cur["YAVG"])
                cur = {}
    n = len(avgs)
    if n == 0:
        raise RuntimeError("Analisis kecerahan gagal (tidak ada frame terbaca).")
    shots, start = [], 0
    for i in range(1, n + 1):
        cut = i == n or (abs(avgs[i] - avgs[i - 1]) > 40 and i - start >= 12) or i - start >= 240
        if cut:
            seg = range(start, i)
            shots.append({
                "start": start, "duration": i - start,
                "metadata_blocks": [{"Level1": {
                    "min_pq": hlg_to_pq(min(mins[j] for j in seg)),
                    "max_pq": hlg_to_pq(max(maxs[j] for j in seg)),
                    "avg_pq": hlg_to_pq(sum(avgs[j] for j in seg) / len(seg))}}]})
            start = i
    return n, shots


def even(n):
    return max(2, int(round(n / 2)) * 2)


def build_vf(o, w, h, hdr):
    f = []
    portrait = h > w
    box = {"4k": (3840, 2160), "1080": (1920, 1080)}.get(o["res"])
    if o["compress"]:
        box = (1920, 1080)
    if box:
        bw, bh = (box[1], box[0]) if portrait else box
        k = min(bw / w, bh / h)
        if o["compress"]:
            k = min(1.0, k)
        if abs(k - 1) > 0.01:
            f.append(f"scale={even(w*k)}:{even(h*k)}:flags=lanczos")
    if o["sharpen"]:
        f.append("hqdn3d=1.2:1.2:3:3")
        f.append("unsharp=5:5:0.9:3:3:0.4")
    if o["boost"] != 1:
        f.append(f"eq=gamma={1/o['boost']:.3f}")
    if hdr:
        f += ["zscale=tin=bt709:t=linear:npl=100", "format=gbrpf32le", "zscale=p=bt2020",
              "zscale=t=arib-std-b67:m=bt2020nc:r=tv", "format=yuv420p10le"]
    else:
        f.append("format=yuv420p")
    return ",".join(f)


def worker(jid, src, o):
    d = os.path.join(WORK, jid)
    p = lambda n: os.path.join(d, n)
    try:
        fps, frames, has_audio, w, h = probe(src)
        out = o["out"]
        hdr = out in ("hdr", "dv")
        vf = build_vf(o, w, h, hdr)
        big = o["res"] == "4k"
        preset = "fast" if big else "medium"
        crf = "24" if o["compress"] else "18"
        cap = ["-maxrate", "8M", "-bufsize", "16M"] if o["compress"] else []

        set_job(jid, stage="Meng-encode video (bagian terlama)", pct=10)
        if out == "sdr":
            cmd = ["ffmpeg", "-y", "-i", src, "-map", "0:v:0", "-map", "0:a:0?", "-vf", vf,
                   "-c:v", "libx264", "-preset", preset, "-crf", crf, *cap,
                   "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", p("output.mp4")]
            run(cmd)
            return set_job(jid, stage="Selesai", pct=100, done=True)

        x265 = ("colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc:"
                "range=limited:repeat-headers=1:aud=1:hrd=0:info=0:chromaloc=2:open-gop=0")
        if o["compress"]:
            x265 += ":vbv-maxrate=8000:vbv-bufsize=16000"
        enc = ["-c:v", "libx265", "-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p10le",
               "-x265-params", x265]

        if out == "hdr":
            run(["ffmpeg", "-y", "-i", src, "-map", "0:v:0", "-map", "0:a:0?", "-vf", vf, *enc,
                 "-tag:v", "hvc1", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                 p("output.mp4")])
            return set_job(jid, stage="Selesai", pct=100, done=True)

        # Dolby Vision profile 8.4
        run(["ffmpeg", "-y", "-i", src, "-an", "-vf", vf, *enc, "-f", "hevc", p("hdr.hevc")])

        set_job(jid, stage="Menganalisis kecerahan per adegan", pct=50)
        base = {"cm_version": "V29", "profile": "8.4",
                "level6": {"max_display_mastering_luminance": 1000,
                           "min_display_mastering_luminance": 1,
                           "max_content_light_level": 1000, "max_average_light_level": 400}}
        try:
            n, shots = analyze_shots(p("hdr.hevc"), p("stats.txt"))
            cfg = dict(base, length=n, shots=shots)
            json.dump(cfg, open(p("dv.json"), "w"))
            set_job(jid, stage=f"Membuat RPU dinamis ({len(shots)} adegan)", pct=60)
            run(["dovi_tool", "generate", "-j", p("dv.json"), "--rpu-out", p("rpu.bin")])
        except Exception:
            # cadangan: RPU statis kalau analisis/format dinamis gagal
            set_job(jid, stage="Membuat RPU (mode statis)", pct=60)
            json.dump(dict(base, length=frames), open(p("dv.json"), "w"))
            run(["dovi_tool", "generate", "-j", p("dv.json"), "--rpu-out", p("rpu.bin")])

        set_job(jid, stage="Menyuntik RPU ke video", pct=70)
        run(["dovi_tool", "inject-rpu", "-i", p("hdr.hevc"), "--rpu-in", p("rpu.bin"), "-o", p("dv.hevc")])

        if has_audio:
            set_job(jid, stage="Memproses audio", pct=80)
            run(["ffmpeg", "-y", "-i", src, "-vn", "-c:a", "aac", "-b:a", "256k", p("audio.m4a")])

        set_job(jid, stage="Mengemas MP4 Dolby Vision", pct=90)
        cmd = ["MP4Box", "-add", f"{p('dv.hevc')}:dvp=8.4:xps_inband:fps={fps}:hdr=none"]
        if has_audio:
            cmd += ["-add", p("audio.m4a")]
        cmd += ["-brand", "mp42isom", "-ab", "dby1", "-new", p("output.mp4")]
        run(cmd)
        for fn in ("hdr.hevc", "dv.hevc", "rpu.bin", "audio.m4a", "stats.txt"):
            if os.path.exists(p(fn)):
                os.remove(p(fn))
        set_job(jid, stage="Selesai", pct=100, done=True)
    except Exception as e:
        set_job(jid, error=str(e), stage="Gagal")


@app.get("/")
def index():
    return send_file(os.path.join(BASE, "index.html"))


@app.post("/upload")
def upload():
    f = request.files.get("video")
    if not f:
        abort(400)
    try:
        boost = min(max(float(request.form.get("boost", 1)), 1.0), 1.6)
    except ValueError:
        boost = 1.0
    jid = uuid.uuid4().hex[:12]
    os.makedirs(os.path.join(WORK, jid))
    src = os.path.join(WORK, jid, "input" + os.path.splitext(f.filename or ".mp4")[1])
    f.save(src)
    name = os.path.splitext(f.filename or "video")[0] + "_DV.mp4"
    jobs[jid] = {"stage": "Antre", "pct": 2, "done": False, "error": None, "name": name}
    o = {"boost": boost,
         "res": request.form.get("res", "orig") if request.form.get("res") in ("orig", "1080", "4k") else "orig",
         "out": request.form.get("out", "dv") if request.form.get("out") in ("sdr", "hdr", "dv") else "dv",
         "compress": request.form.get("compress") == "1",
         "sharpen": request.form.get("sharpen") == "1"}
    threading.Thread(target=worker, args=(jid, src, o), daemon=True).start()
    return jsonify(id=jid)


@app.get("/status/<jid>")
def status(jid):
    if jid not in jobs:
        abort(404)
    return jsonify(jobs[jid])


@app.get("/download/<jid>")
def download(jid):
    if jid not in jobs or not jobs[jid]["done"]:
        abort(404)
    return send_file(os.path.join(WORK, jid, "output.mp4"),
                     as_attachment=True, download_name=jobs[jid]["name"])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
