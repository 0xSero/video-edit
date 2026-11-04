#!/usr/bin/env python3
"""verify_output.py job.json [--sheet START:DUR ...]

Delivery gate. Prints PASS/FAIL and exits non-zero on FAIL:

  * frame count == sum of segment frames
  * every audio track within one frame of the video duration
  * OCR at 4 fps across every redaction window (job.blur, job.boxes,
    job.verify_windows) finds no hostnames / IPs / emails / keys

--sheet START:DUR renders a 0.5 fps contact sheet of the OUTPUT, e.g. --sheet 0:60 to
eyeball the opening. Frames live in out_sheet_<start>/ and the sheet next to it.
"""
import glob
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import (enter_job, frame_rate, grid, media_size, out_time,
                     read_segments, stream_durations, video_frames)
from ocr_scan import PATS, benign, ocr_files


def sheet(path, spec):
    start, dur = (float(x) for x in spec.split(":"))
    work = f"out_sheet_{int(start)}"
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(start),
                    "-t", str(dur), "-i", path, "-vf", "fps=0.5", "-q:v", "3",
                    f"{work}/o_%04d.jpg"], check=True)
    files = sorted(glob.glob(f"{work}/*.jpg"))
    if not files:
        print(f"no frames for {spec}")
        return
    print("wrote", f"{work}.jpg", grid([(f, start + k * 2) for k, f in enumerate(files)],
                                       f"{work}.jpg", 320, 180, 6))


def leak_windows(job, segs):
    spans = [(w["t0"] - 3, w["t1"] + 3) for w in job.get("blur", [])]
    spans += [(b["t0"] - 3, b["t1"] + 3) for b in job.get("boxes", [])]
    spans += [tuple(w) for w in job.get("verify_windows", [])]
    out = []
    for s0, s1 in spans:
        times = [out_time(segs, s0 + i / 2) for i in range(int((s1 - s0) * 2) + 1)]
        if times:
            out.append((max(0.0, min(times) - 0.5), max(times) + 0.5))
    return out


def main():
    job = enter_job(sys.argv[1])
    if "--sheet" in sys.argv:
        for spec in sys.argv[sys.argv.index("--sheet") + 1:]:
            sheet(job["output"], spec)
        return

    segs = read_segments(job)
    fr = frame_rate(job["output"])  # render preserves the source rate
    expected = sum(round((b - a) * fr) for a, b in segs)
    actual = video_frames(job["output"])
    failures = []
    if actual != expected:
        failures.append(f"frames {actual} != {expected}")

    durations = stream_durations(job["output"])
    video = next((d for kind, d in durations if kind == "video"), None)
    audio = [d for kind, d in durations if kind == "audio"]
    tolerance = max(0.1, 2 / fr)
    if video is None:
        failures.append("no video stream")
    for i, d in enumerate(audio):
        if video is not None and abs(d - video) > tolerance:
            failures.append(f"audio track {i} {d:.2f}s vs video {video:.2f}s")
    if len(audio) != 2:
        failures.append(f"{len(audio)} audio tracks, expected 2 (enhanced + original)")
    print(f"frames {actual} (expected {expected}); video {video:.2f}s; "
          f"audio {[round(a, 2) for a in audio]}")

    windows = leak_windows(job, segs)
    fps = 4
    if "--full" in sys.argv:
        windows = [(0.0, video or 0.0)]
        fps = 1
    shutil.rmtree("vframes", ignore_errors=True)
    os.makedirs("vframes")
    for i, (a, b) in enumerate(windows):
        if b - a < 0.3:
            continue
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", job["output"],
                        "-vf", f"fps={fps}", "-q:v", "3", f"vframes/w{i}_%05d.jpg"],
                       check=True)

    width, height = media_size(job["output"])
    files = sorted(glob.glob("vframes/*.jpg"))
    leaks = []
    frames = ocr_files(files)["frames"] if files else {}
    for name, items in frames.items():
        for text, *_ in items:
            for kind, pat in PATS.items():
                for m in pat.finditer(text):
                    if not benign(kind, m.group(0).lstrip(" ."), text):
                        leaks.append(f"{kind}: {m.group(0)} in {name!r} {text[:60]!r}")
    if leaks:
        failures.append(f"{len(leaks)} leak hit(s)")

    print(f"windows (output seconds): {[(round(a, 1), round(b, 1)) for a, b in windows]}")
    print(f"frames checked: {len(files)} at {width}x{height}; leaks: {len(leaks)}")
    for line in leaks[:20]:
        print("  ", line)
    if failures:
        print("FAIL:", "; ".join(failures))
        sys.exit(1)
    print("PASS: frame count, two audio tracks, no leaks in the redaction windows")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__.strip())
    main()
