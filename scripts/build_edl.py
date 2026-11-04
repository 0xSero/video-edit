#!/usr/bin/env python3
"""build_edl.py job.json  ->  segments.json

Turns manual content decisions (job.keep / job.cuts) into render segments:

  1. subtract privacy cuts from the keep ranges
  2. tighten pauses inside each kept range
       - silencedetect on the *enhanced* audio (-42 dB, 0.5 s)
       - whisper word gaps longer than pause.max where the enhanced RMS stays below
         pause.loud_db (typing, fan noise and video playback are not pauses)
  3. snap boundaries to the source frame grid, drop slivers shorter than 0.3 s
"""
import json
import os
import re
import subprocess
import sys

import numpy as np
import soundfile as sf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import enter_job, frame_rate

MIN_SEG = 0.3


def subtitles(path, thr, dur, cache="silences_df.txt"):
    """[(start, end)] silence ranges detected by ffmpeg, cached for reruns."""
    if not os.path.exists(cache):
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", path, "-af",
             f"silencedetect=noise={thr}dB:d={dur}", "-f", "null", "-"],
            capture_output=True, text=True,
        )
        if r.returncode:
            sys.exit(f"silencedetect failed on {path}:\n{r.stderr[-800:]}")
        open(cache, "w").write(
            "\n".join(l for l in r.stderr.splitlines() if "silence_" in l))
    text = open(cache).read()
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", text)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", text)]
    # a trailing silence has a start and no end: zip() truncates it, as intended
    return list(zip(starts, ends))


def minus(keep, cuts):
    """keep ranges minus merged, sorted cuts."""
    merged = []
    for c0, c1 in sorted(tuple(c) for c in cuts):
        if merged and c0 <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], c1)
        else:
            merged.append([c0, c1])
    out = []
    for a, b in keep:
        cur = a
        for c0, c1 in merged:
            if c1 <= cur:
                continue
            if c0 >= b:
                break
            if c0 > cur:
                out.append((cur, c0))
            cur = max(cur, c1)
            if cur >= b:
                break
        if cur < b:
            out.append((cur, b))
    return [(a, b) for a, b in out if b - a > 0.05]


def main():
    job = enter_job(sys.argv[1])
    fr = frame_rate("src.mov")
    pause = job.get("pause", {})
    maxp = float(pause.get("max", 0.7))
    keepp = float(pause.get("keep", 0.4))
    loud_db = float(pause.get("loud_db", -26))
    enh = job.get("enhanced_wav", "df_enhanced.wav")

    keep = minus([tuple(k) for k in job.get("keep", [])], job.get("cuts", []))
    if not keep:
        sys.exit("job.keep did not survive job.cuts - nothing left to render")
    if not os.path.exists(enh):
        sys.exit(f"{enh} missing - run: df_enhance.py orig48.wav {enh}")
    if not os.path.exists("transcript.json"):
        print("! transcript.json missing - pause tightening from silencedetect only")

    # loudness envelope of the enhanced track, one value per 10 ms
    audio, sr = sf.read(enh, dtype="float32")
    if audio.ndim == 2:
        audio = audio.mean(1)
    hop = max(1, sr // 100)
    n = len(audio) // hop
    db = 20 * np.log10(np.sqrt((audio[: n * hop].reshape(n, hop).astype(np.float64) ** 2)
                               .mean(1)) + 1e-9)

    def audible(s, e):
        i, j = max(0, int(s * 100)), max(0, int(e * 100))
        return j > i and bool(db[i:j].max() > loud_db)

    words = []
    if os.path.exists("transcript.json"):
        data = json.load(open("transcript.json"))
        words = sorted((w["start"], w["end"])
                       for s in data.get("segments", [])
                       for w in s.get("words", []) if w.get("word", "").strip())
        if not words:
            print("! transcript has no word timestamps - re-run whisper with --word-timestamps")

    silences = subtitles(enh, -42, 0.5)
    gaps = [(e1 + 0.1, s2 - 0.1)
            for (_, e1), (s2, _) in zip(words, words[1:])
            if s2 - e1 > maxp and not audible(e1 + 0.15, s2 - 0.15)]

    merged = []
    for s, e in sorted(silences + gaps):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))

    segs = []
    for a, b in keep:
        cur = a
        for s, e in merged:
            s2, e2 = max(s, a), min(e, b)
            if e2 - s2 > maxp:
                segs.append((cur, s2 + keepp / 2))
                cur = e2 - keepp / 2
        segs.append((cur, b))

    out = []
    for s, e in segs:
        s, e = round(s * fr) / fr, round(e * fr) / fr
        if e - s < MIN_SEG:
            continue
        if out and s - out[-1][1] < 1 / fr:
            out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))

    json.dump(out, open("segments.json", "w"))
    kept = sum(b - a for a, b in keep)
    final = sum(b - a for a, b in out)
    print(f"keep {len(keep)} ranges = {kept / 60:.1f} min; pauses: {len(silences)} silence "
          f"+ {len(gaps)} word-gap -> {len(out)} segments = {final / 60:.1f} min "
          f"(target {job.get('target_minutes', '?')})")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__.strip())
    main()
