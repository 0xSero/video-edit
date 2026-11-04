#!/usr/bin/env python3
"""chapters.py job.json - YouTube chapter list, in output time.

job.chapters = [[source_seconds, "Title"], ...]. A timestamp inside a cut snaps to the
next kept moment; chapters closer than 10 s to the previous one are dropped because
YouTube ignores them anyway.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import enter_job, out_time, read_segments

MIN_GAP = 10


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__.strip())
    job = enter_job(sys.argv[1])
    segs = read_segments(job)
    chapters = sorted((float(t), title) for t, title in job.get("chapters", []))
    if not chapters:
        sys.exit("job.chapters is empty - add [[source_seconds, \"Title\"], ...]")
    last = -1e9
    for source_t, title in chapters:
        t = out_time(segs, source_t)
        if t - last < MIN_GAP:
            print(f"   (dropped, <{MIN_GAP}s after previous: {title})")
            continue
        print(f"{int(t // 60)}:{int(t % 60):02d} {title}")
        last = t
    total = sum(b - a for a, b in segs)
    print(f"-- runtime {int(total // 60)}:{int(total % 60):02d}")


if __name__ == "__main__":
    main()
