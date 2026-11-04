#!/usr/bin/env python3
"""ocr_scan.py - Apple Vision text recognition + PII scan over extracted frames.

  ocr_scan.py run   <frames_dir> <out.json> [--shards N]   OCR every jpg in parallel
  ocr_scan.py scan  <ocr.json>                              print PII hits with pixel boxes
  ocr_scan.py dense <src.mov> <dense_dir> START:DUR ...     2 fps frames for ranges, then OCR

Frames keep their timestamp in the filename: f_NNNNN.jpg = (N-1)*2 s at 0.5 fps
(prep.sh), d_NNNNNN.jpg = N/2 s at 2 fps (dense, --start_number S*2).
The JSON layout is {"size": [w, h], "frames": {name: [[text, x, y, w, h], ...]}} with
normalized Vision bounding boxes (origin bottom-left); scan converts to pixels.
"""
import glob
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import frame_time

PATS = {
    "ipv4": re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])"),
    "ipv6": re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){3,7}[0-9a-fA-F]{1,4}\b"),
    "tsnet": re.compile(r"[\w.-]+\.ts\.net", re.I),
    "email": re.compile(r"[A-Za-z][\w.+-]+@[A-Za-z][\w-]+\.[A-Za-z]{2,}"),
    "key": re.compile(r"\b(sk-[\w-]{10,}|hf_[A-Za-z0-9]{10,}|gh[pousr]_[A-Za-z0-9]{10,}"
                      r"|xox[abp]-[\w-]{10,}|AKIA[0-9A-Z]{12,}|tskey-[\w-]{10,})"),
    "ssh": re.compile(r"\bssh\s+\S+@\S+", re.I),
    "privip": re.compile(r"100[.,]\d{1,3}[.,]\d{1,3}[.,]\d{1,3}|192[.,]168[.,]\d+"
                         r"|(?:^|[^\d.])10[.,]\d{1,3}[.,]\d{1,3}[.,]\d{1,3}"),
    "secret": re.compile(r"(bearer\s+\S{10,}|api[_-]?key\s*[:=]\s*\S{8,}|password\s*[:=]\s*\S+)",
                         re.I),
}


def benign(kind, value, line=""):
    """Loopback, localhost and clock-style 'a:b:c' strings are not secrets."""
    if kind == "ipv4":
        if value in ("127.0.0.1", "0.0.0.0") or any(int(q) > 255 for q in value.split(".")):
            return True
        return bool(re.fullmatch(r"\d\.\d\.\d\.\d", value))
    if kind == "ipv6":
        groups = value.split(":")
        return all(len(g) <= 2 and int(g, 16) < 100 for g in groups if g) and "::" not in value
    if kind in ("secret", "email", "ssh") and ("localhost" in line or "127.0.0.1" in line):
        return True
    return False


def ocr_files(files):
    import Vision
    import Quartz
    from Foundation import NSURL
    from PIL import Image

    size = None
    frames = {}
    for path in files:
        if size is None:
            size = Image.open(path).size
        url = NSURL.fileURLWithPath_(os.path.abspath(path))
        src = Quartz.CGImageSourceCreateWithURL(url, None)
        image = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        req.setUsesLanguageCorrection_(False)
        Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, None) \
            .performRequests_error_([req], None)
        items = []
        for obs in (req.results() or []):
            box = obs.boundingBox()
            items.append([obs.topCandidates_(1)[0].string(),
                          round(box.origin.x, 4), round(box.origin.y, 4),
                          round(box.size.width, 4), round(box.size.height, 4)])
        frames[os.path.basename(path)] = items
    return {"size": list(size or (1920, 1080)), "frames": frames}


def ocr_dir(frames_dir, out_json, shards=8):
    files = sorted(glob.glob(os.path.join(frames_dir, "*.jpg")))
    if not files:
        sys.exit(f"no frames in {frames_dir}")
    shards = max(1, min(shards, len(files)))
    procs = [subprocess.Popen([sys.executable, os.path.abspath(__file__), "shard",
                               frames_dir, out_json, str(i), str(shards)])
             for i in range(shards)]
    for p in procs:
        if p.wait():
            sys.exit(f"OCR shard failed (exit {p.returncode})")
    merged = {"size": [], "frames": {}}
    for i in range(shards):
        part = f"{out_json}.{i}"
        data = json.load(open(part))
        merged["size"] = merged["size"] or data["size"]
        merged["frames"].update(data["frames"])
        os.remove(part)
    json.dump(merged, open(out_json, "w"))
    print("frames", len(merged["frames"]),
          "lines", sum(len(v) for v in merged["frames"].values()), "->", out_json)
    return merged


def scan(ocr_json):
    data = json.load(open(ocr_json))
    width, height = data["size"]
    hits = {}
    for name, items in data["frames"].items():
        t = frame_time(name)
        for text, x, y, w, h in items:
            for kind, pat in PATS.items():
                for m in pat.finditer(text):
                    value = m.group(0).lstrip(" .")
                    if benign(kind, value, text):
                        continue
                    box = (int(x * width), int((1 - y - h) * height),
                           int(w * width) + 1, int(h * height) + 1)
                    hits.setdefault((kind, value), []).append((t, box, text[:70]))
    if not hits:
        print("no hits")
        return {}
    for (kind, value), found in sorted(hits.items(), key=lambda kv: kv[1][0][0]):
        times = [f[0] for f in found]
        print(f"{kind:7s} {value:45s} n={len(found):3d} "
              f"{int(times[0] // 60):02d}:{times[0] % 60:04.1f}-"
              f"{int(times[-1] // 60):02d}:{times[-1] % 60:04.1f}")
        for t, (bx, by, bw, bh), text in found[:6]:
            print(f"        t={t:7.1f} box x={bx} y={by} w={bw} h={bh} | {text}")
        if len(found) > 6:
            print(f"        ... {len(found) - 6} more")
    return hits


def dense(src, ddir, ranges):
    os.makedirs(ddir, exist_ok=True)
    procs = []
    for r in ranges:
        start, dur = (int(x) for x in r.split(":"))
        procs.append(subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(start),
             "-t", str(dur), "-i", src, "-vf", "fps=2", "-q:v", "3",
             "-start_number", str(start * 2), f"{ddir}/d_%06d.jpg"]))
    for p in procs:
        p.wait()
    return ocr_dir(ddir, f"{ddir}_all.json")


def main(argv):
    if len(argv) < 2:
        sys.exit(__doc__.strip())
    cmd = argv[1]
    if cmd == "shard":
        fdir, out, i, n = argv[2], argv[3], int(argv[4]), int(argv[5])
        json.dump(ocr_files(sorted(glob.glob(f"{fdir}/*.jpg"))[i::n]), open(f"{out}.{i}", "w"))
    elif cmd == "run":
        shards = int(argv[argv.index("--shards") + 1]) if "--shards" in argv else 8
        ocr_dir(argv[2], argv[3], shards)
    elif cmd == "scan":
        scan(argv[2])
    elif cmd == "dense":
        if len(argv) < 4:
            sys.exit(__doc__.strip())
        dense(argv[2], argv[3], argv[4:])
    else:
        sys.exit(__doc__.strip())


if __name__ == "__main__":
    main(sys.argv)
