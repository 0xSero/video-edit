#!/usr/bin/env python3
"""render.py job.json [--force] [--dry]

Segments (segments.json) -> finished mp4 in one pass, restartable per group:

  1. split segments into render_groups source-time buckets (bounds cached in
     group_bounds.json so a rerun keeps the same buckets)
  2. per group build a filter script (drawbox fills + whole-pane blur windows +
     select/setpts) and encode h264 in parallel; a group whose filter script and frame
     count are unchanged is skipped
  3. concat parts with -c copy, assert total frame count
  4. cut both audio tracks with the same segments (6 ms edge fades)
  5. EQ/compression + two-pass loudnorm on the enhanced track
  6. mux: video + enhanced (default) + untouched mic (track 2), faststart

--force  re-encode every group        --dry  write filter scripts only, render nothing
"""
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import enter_job, frame_rate, read_segments, video_frames

BLUR = "boxblur=luma_radius=22:luma_power=2:chroma_radius=8:chroma_power=1"
DEFAULT_CHAIN = (
    "highpass=f=75,"
    "equalizer=f=220:t=q:w=1.2:g=-2.5,"
    "equalizer=f=3200:t=q:w=1:g=2,"
    "equalizer=f=9000:t=q:w=1:g=1,"
    "deesser=i=0.3,"
    "acompressor=threshold=-20dB:ratio=3:attack=6:release=120:makeup=1.6"
)
FADE = 0.006


def filter_option():
    """ffmpeg 7+ reads graph files with -/filter:v; 6.x only knows -filter_script:v."""
    try:
        banner = subprocess.run(["ffmpeg", "-version"], capture_output=True,
                                text=True).stdout.splitlines()[0]
        major = int(re.search(r"ffmpeg version n?(\d+)", banner).group(1))
    except (IndexError, AttributeError, ValueError):
        major = 0
    return "-/filter:v" if major >= 7 else "-filter_script:v"


def build_graph(segs, ss, to, job, fr):
    """Filter script for one group, relative to the group's first source frame."""
    prev = "0:v"
    chains = []

    draws = [b for b in job.get("boxes", []) if b["t1"] >= ss and b["t0"] <= to]
    if draws:
        expr = ",".join(
            f"drawbox=x={b['x']}:y={b['y']}:w={b['w']}:h={b['h']}:color=0x202225:t=fill:"
            f"enable='between(t,{b['t0'] - ss:.3f},{b['t1'] - ss:.3f})'"
            for b in draws
        )
        chains.append(f"[{prev}]{expr}[d]")
        prev = "d"

    windows = [w for w in job.get("blur", []) if w["t1"] >= ss and w["t0"] <= to]
    if windows:
        regions = job.get("blur_regions")
        if not regions:
            sys.exit("job.blur is set but job.blur_regions is missing: list the "
                     "[x, y, w, h] panes to blur in source pixels")
        enable = "+".join(f"between(t,{w['t0'] - ss:.3f},{w['t1'] - ss:.3f})" for w in windows)
        for i, (x, y, w, h) in enumerate(regions):
            chains.append(f"[{prev}]split[a{i}][b{i}];[b{i}]crop={w}:{h}:{x}:{y},{BLUR}[c{i}];"
                          f"[a{i}][c{i}]overlay={x}:{y}:enable='{enable}'[v{i}]")
            prev = f"v{i}"

    select = "+".join(f"between(t,{a - ss - 0.004:.4f},{b - ss - 0.004:.4f})" for a, b in segs)
    chains.append(f"[{prev}]select='{select}',setpts=N/{fr}/TB,format=yuv420p")
    return ";\n".join(chains)


def cut_audio(src, dst, segs, sr_expected):
    """Stream the segments out of one audio file, 6 ms fades, exact segment lengths."""
    import numpy as np
    import soundfile as sf

    info = sf.info(src)
    if abs(info.samplerate - sr_expected) > 1:
        sys.exit(f"{src} is {info.samplerate} Hz, expected {sr_expected}")
    fade = max(1, int(FADE * info.samplerate))
    ramp = np.linspace(0, 1, fade, dtype="float32")
    if info.channels > 1:
        ramp = ramp[:, None]
    written = 0
    with sf.SoundFile(src) as f, sf.SoundFile(dst, "w", info.samplerate,
                                              info.channels, "PCM_16") as o:
        for a, b in segs:
            start = int(round(a * info.samplerate))
            want = int(round((b - a) * info.samplerate))
            f.seek(start)
            data = f.read(want, dtype="float32", always_2d=info.channels > 1)
            if len(data) < want:
                print(f"! {src}: segment {a:.2f} ran past EOF, padding {want - len(data)} samples")
                pad = np.zeros((want - len(data),) + data.shape[1:], dtype="float32")
                data = np.concatenate([data, pad])
            n = min(fade, len(data))
            data[:n] *= ramp[:n]
            data[-n:] *= ramp[:n][::-1]
            o.write(data)
            written += len(data)
    print(f"{dst}: {written / info.samplerate:.2f} s from {len(segs)} segments")


def main():
    job = enter_job(sys.argv[1])
    force = "--force" in sys.argv
    dry = "--dry" in sys.argv
    if not os.path.exists("src.mov"):
        sys.exit("src.mov missing - run prep.sh <source> <workdir> first")

    fr = frame_rate("src.mov")
    segs = read_segments(job)
    groups_wanted = max(1, int(job.get("render_groups", 4)))
    bitrate = job.get("video_bitrate", "14M")
    encoder = job.get("encoder", "h264_videotoolbox")

    bounds_path = "group_bounds.json"
    if os.path.exists(bounds_path) and not force:
        bounds = json.load(open(bounds_path))
    else:
        k = min(groups_wanted, len(segs))
        bounds = [segs[i * len(segs) // k][0] - 0.01 for i in range(1, k)]
        json.dump(bounds, open(bounds_path, "w"))
    edges = [0] + bounds + [1e9]
    groups = [[s for s in segs if edges[i] <= s[0] < edges[i + 1]] for i in range(len(edges) - 1)]

    running = []
    for g, gs in enumerate(groups):
        if not gs:
            continue
        need = sum(round((b - a) * fr) for a, b in gs)
        ss, to = max(0.0, gs[0][0] - 0.5), gs[-1][1] + 0.5
        graph = build_graph(gs, ss, to, job, fr)
        script = f"vf_{g}.txt"
        previous = open(script).read() if os.path.exists(script) else None
        part = f"part_{g}.mp4"
        if (not force and previous == graph and os.path.exists(part)
                and video_frames(part) == need):
            print(f"group {g}: unchanged ({need} frames), skipping")
            continue
        open(script, "w").write(graph)
        cmd = ["ffmpeg", "-hide_banner", "-y", "-hwaccel", "videotoolbox",
               "-ss", f"{ss:.4f}", "-to", f"{to:.4f}", "-i", "src.mov",
               filter_option(), script, "-an", "-c:v", encoder, "-b:v", bitrate,
               "-maxrate", job.get("video_maxrate", "18M"),
               "-bufsize", job.get("video_bufsize", "28M"),
               "-profile:v", "high", "-r", f"{fr:g}", part]
        print(f"group {g}: {len(gs)} segs, {need} frames, source {ss:.1f}-{to:.1f} s")
        if dry:
            print("   ", " ".join(cmd))
            continue
        running.append((g, need, subprocess.Popen(
            cmd, stdout=open(f"render_{g}.log", "w"), stderr=subprocess.STDOUT)))
    if dry:
        print("dry run: filter scripts written, nothing rendered")
        return
    for g, need, proc in running:
        proc.wait()
        got = video_frames(f"part_{g}.mp4")
        print(f"group {g}: {got} frames (need {need})", "OK" if got == need else "MISMATCH")
        if got != need:
            sys.exit(f"group {g} frame mismatch - see render_{g}.log")

    with open("concat.txt", "w") as f:
        for g, gs in enumerate(groups):
            if gs:
                f.write(f"file 'part_{g}.mp4'\n")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat",
                    "-safe", "0", "-i", "concat.txt", "-c", "copy", "video_cut.mp4"], check=True)
    expected = sum(round((b - a) * fr) for a, b in segs)
    got = video_frames("video_cut.mp4")
    print(f"video frames {got}, expected {expected}")
    if got != expected:
        sys.exit("concat frame count mismatch")

    import soundfile as sf
    sr = sf.info("orig48.wav").samplerate
    cut_audio("orig48.wav", "cut_orig.wav", segs, sr)
    cut_audio(job.get("enhanced_wav", "df_enhanced.wav"), "cut_enh.wav", segs, sr)

    loud = job.get("loudnorm", {"I": -16, "TP": -1.5, "LRA": 11})
    target = f"I={loud['I']}:TP={loud['TP']}:LRA={loud['LRA']}"
    chain = job.get("audio_chain", DEFAULT_CHAIN)
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", "cut_enh.wav", "-af",
                        f"{chain},loudnorm={target}:print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"loudnorm pass 1 failed:\n{r.stderr[-1200:]}")
    measured = json.loads(r.stderr[r.stderr.rfind("{"):r.stderr.rfind("}") + 1])
    second = (f"loudnorm={target}:measured_I={measured['input_i']}:"
              f"measured_TP={measured['input_tp']}:measured_LRA={measured['input_lra']}:"
              f"measured_thresh={measured['input_thresh']}:"
              f"offset={measured['target_offset']}:linear=true")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-i", "cut_enh.wav", "-af", f"{chain},{second},aresample=48000",
                    "-ar", "48000", "enh_final.wav"], check=True)
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", "enh_final.wav", "-af",
                        "ebur128=peak=true", "-f", "null", "-"],
                       capture_output=True, text=True)
    print("enhanced:", [l.strip() for l in r.stderr.splitlines()
                        if l.strip().startswith(("I:", "Peak:"))])

    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-i", "video_cut.mp4", "-i", "enh_final.wav", "-i", "cut_orig.wav",
                    "-map", "0:v", "-map", "1:a", "-map", "2:a",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ac:a:0", "2",
                    "-metadata:s:a:0", "title=Enhanced (DeepFilterNet + EQ + loudnorm)",
                    "-metadata:s:a:0", "language=eng", "-disposition:a:0", "default",
                    "-metadata:s:a:1", "title=Original mic",
                    "-metadata:s:a:1", "language=eng", "-disposition:a:1", "0",
                    "-movflags", "+faststart", job["output"]], check=True)
    subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                    "stream=index,codec_type,duration", "-of", "compact", job["output"]])
    print("wrote", job["output"], os.path.getsize(job["output"]) // 2 ** 20, "MB")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__.strip())
    main()
