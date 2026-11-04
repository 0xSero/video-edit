"""Shared helpers for the video-edit kit. stdlib + numpy/Pillow only.

Nothing here knows about any particular job; every script imports from this module so
frame-time, source->output time mapping and contact sheets exist exactly once.
"""
import json
import os
import re
import subprocess
import sys


def enter_job(job_path):
    """Load job.json, absolutize workdir/output, chdir into the workdir."""
    if not os.path.exists(job_path):
        sys.exit(f"job file not found: {job_path}")
    job = json.load(open(job_path))
    for key in ("workdir", "output"):
        if key not in job:
            sys.exit(f"{job_path}: missing required key '{key}'")
    wd = os.path.abspath(job["workdir"])
    os.makedirs(wd, exist_ok=True)
    os.chdir(wd)
    job["workdir"] = wd
    job["output"] = os.path.abspath(job["output"])
    return job


def read_segments(job):
    path = os.path.join(job["workdir"], "segments.json")
    if not os.path.exists(path):
        sys.exit("segments.json missing - run build_edl.py first")
    segs = [(float(a), float(b)) for a, b in json.load(open(path))]
    if not segs:
        sys.exit("segments.json is empty")
    return segs


def out_time(segs, t):
    """Map a source timestamp to the output timeline."""
    acc = 0.0
    for a, b in segs:
        if t < a:
            return acc
        if t <= b:
            return acc + (t - a)
        acc += b - a
    return acc


def frame_time(name):
    """Frame filename -> source seconds. f_ = 0.5 fps (prep.sh), d_ = 2 fps (dense)."""
    base = os.path.basename(name)
    m = re.match(r"^([fd])_(\d+)\.(?:jpg|png)$", base)
    if not m:
        raise ValueError(f"unrecognized frame name: {base}")
    n = int(m.group(2))
    return (n - 1) * 2.0 if m.group(1) == "f" else n / 2.0


def _ffprobe(path, entries):
    return subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", f"stream={entries}", "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True,
    ).stdout.split()


def media_size(path):
    """(width, height) of the first video stream."""
    try:
        w, h = _ffprobe(path, "width,height")[:2]
        return int(w), int(h)
    except (ValueError, IndexError):
        sys.exit(f"cannot read video dimensions: {path}")


def frame_rate(path):
    """Frame rate as a float, e.g. 60000/1001 -> 59.94."""
    try:
        raw = _ffprobe(path, "r_frame_rate")[0]
        num, _, den = raw.partition("/")
        return float(num) / float(den or 1)
    except (ValueError, IndexError):
        sys.exit(f"cannot read frame rate: {path}")


def video_frames(path):
    """Decoded packet count of the first video stream (0 if unreadable)."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
         "-show_entries", "stream=nb_read_packets", "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True,
    )
    return int(r.stdout.strip() or 0)


def stream_durations(path):
    """[(codec_type, seconds)] for every stream that reports a duration."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration",
         "-of", "compact=p=0:nk=0", path],
        capture_output=True, text=True,
    ).stdout
    out = []
    for line in r.strip().splitlines():
        fields = dict(part.split("=", 1) for part in line.split("|") if "=" in part)
        try:
            out.append((fields["codec_type"], float(fields["duration"])))
        except (KeyError, ValueError):
            continue
    return out


def fmt_t(t):
    return f"{int(t // 60):02d}:{t % 60:04.1f}"


def grid(items, out, tw=270, th=160, cols=10, label=fmt_t, quality=85):
    """Contact sheet of [(path, seconds)]. Returns the sheet size."""
    from PIL import Image, ImageDraw
    if not items:
        sys.exit("grid(): no frames")
    rows = (len(items) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * tw, rows * (th + 14)), "black")
    draw = ImageDraw.Draw(sheet)
    for k, (path, t) in enumerate(items):
        x, y = (k % cols) * tw, (k // cols) * (th + 14)
        sheet.paste(Image.open(path).convert("RGB").resize((tw, th)), (x, y))
        draw.text((x + 3, y + th), label(t), fill="yellow")
    sheet.save(out, quality=quality)
    return sheet.size
