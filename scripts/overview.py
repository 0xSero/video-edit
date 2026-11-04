#!/usr/bin/env python3
"""overview.py <frames_dir> <out.jpg> [seconds_per_frame] [cols]

Contact sheet of up to 24 evenly spaced frames from prep.sh's frames/ directory.
"""
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import fmt_t, grid


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__.strip())
    frames_dir, out = sys.argv[1], sys.argv[2]
    spf = float(sys.argv[3]) if len(sys.argv) > 3 else 2.0
    cols = int(sys.argv[4]) if len(sys.argv) > 4 else 4
    files = sorted(glob.glob(os.path.join(frames_dir, "f_*.jpg")))
    if len(files) < 2:
        sys.exit(f"need at least 2 frames in {frames_dir}")
    n = min(24, len(files))
    picks = [round(i * (len(files) - 1) / (n - 1)) for i in range(n)]
    size = grid([(files[i], i * spf) for i in picks], out, 480, 270, cols, fmt_t)
    print(f"frames {len(files)} -> {out} {size[0]}x{size[1]}")


if __name__ == "__main__":
    main()
