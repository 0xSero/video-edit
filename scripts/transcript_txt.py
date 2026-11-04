#!/usr/bin/env python3
"""transcript_txt.py <job.json | workdir> [--from SEC] [--to SEC]

Prints the whisper transcript as one line per segment with source timestamps, which is
what you actually read when deciding what to keep. Times are SOURCE seconds until you
re-transcribe the cut (see SKILL.md step 12).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import fmt_t


def main():
    args = [a for a in sys.argv[1:]]
    if not args:
        sys.exit(__doc__.strip())
    target = args.pop(0)
    if target.endswith(".json"):
        job = json.load(open(target))
        workdir = job["workdir"]
        lo = float(job.get("transcript_from", 0))
        hi = float(job.get("transcript_to", 1e9))
    else:
        workdir = target
        lo, hi = 0.0, 1e9
    if "--from" in args:
        lo = float(args[args.index("--from") + 1])
    if "--to" in args:
        hi = float(args[args.index("--to") + 1])

    path = os.path.join(workdir, "transcript.json")
    if not os.path.exists(path):
        sys.exit(f"{path} missing - prep.sh starts whisper, check whisper.log")
    data = json.load(open(path))
    if not data.get("segments"):
        sys.exit(f"{path} has no segments - whisper may still be running")
    for seg in data["segments"]:
        if seg["end"] < lo or seg["start"] > hi:
            continue
        print(f"[{fmt_t(seg['start'])}-{fmt_t(seg['end'])}] {seg['text'].strip()}")


if __name__ == "__main__":
    main()
