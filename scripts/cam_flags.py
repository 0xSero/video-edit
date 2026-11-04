#!/usr/bin/env python3
"""cam_flags.py <workdir> [--sheet S:DUR ...] [--range S:E]

Webcam privacy helper. Everything reads the crop geometry prep.sh wrote to
webcam_crop.txt, so it works for any resolution.

  (no flag)       Vision hand-pose + face detection over camall/ (1 fps crops):
                  writes handface.json, flag_N.jpg sheets and prints clusters of frames
                  where a hand sits at the mouth.
  --sheet S:DUR   zoomed 2 fps webcam sheets for source ranges -> zoom.jpg
  --range S:E     1 fps sheet of existing camall crops -> cam_range.jpg

Expect roughly a fifth of the frames flagged: talking with your hands counts too. The
clusters are the signal, then you LOOK at them zoomed before cutting anything.
"""
import glob
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import grid

CONF = 0.3  # hand joint confidence floor
COLUMNS = 10


def crop():
    if not os.path.exists("webcam_crop.txt"):
        sys.exit("webcam_crop.txt missing - run prep.sh first (or set WEBCAM_CROP)")
    return open("webcam_crop.txt").read().strip()


def frames_for(start, end):
    have = sorted(glob.glob("camall/c_*.jpg"))
    return [(f, t) for f in have for t in [int(f.split("_")[-1][:5]) - 1] if start <= t <= end]


def sheet(start, dur, out, fps=2, tw=360, th=213, cols=8):
    os.makedirs("zoom", exist_ok=True)
    for old in glob.glob("zoom/*.jpg"):
        os.remove(old)
    tag = f"z{int(start)}"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", str(start), "-t", str(dur), "-i", "src.mov",
                    "-vf", f"fps={fps},crop={crop()}", "-q:v", "3",
                    f"zoom/{tag}_%03d.jpg"], check=True)
    items = [(f, start + int(os.path.basename(f).split("_")[-1][:3]) / fps - 1 / fps)
             for f in sorted(glob.glob(f"zoom/{tag}_*.jpg"))]
    print("wrote", out, grid(items, out, tw, th, cols) if items else None)


def detect():
    import Vision
    import Quartz
    from Foundation import NSURL

    result = {}
    for path in sorted(glob.glob("camall/c_*.jpg")):
        t = int(path.split("_")[-1][:5]) - 1
        url = NSURL.fileURLWithPath_(os.path.abspath(path))
        src = Quartz.CGImageSourceCreateWithURL(url, None)
        image = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
        faces_req = Vision.VNDetectFaceRectanglesRequest.alloc().init()
        hands_req = Vision.VNDetectHumanHandPoseRequest.alloc().init()
        hands_req.setMaximumHandCount_(2)
        Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, None) \
            .performRequests_error_([faces_req, hands_req], None)

        faces = [(o.boundingBox().origin.x, o.boundingBox().origin.y,
                  o.boundingBox().size.width, o.boundingBox().size.height)
                 for o in (faces_req.results() or [])]
        hands = []
        for obs in (hands_req.results() or []):
            points, _ = obs.recognizedPointsForGroupKey_error_(
                Vision.VNHumanHandPoseObservationJointsGroupNameAll, None)
            if points:
                hands.append([(p.location().x, p.location().y)
                              for p in points.values() if p.confidence() > CONF])
        near = 0
        for fx, fy, fw, fh in faces:
            cx, cy = fx + fw / 2, fy + fh * 0.3  # mouth line sits high in a Vision face box
            near += sum(1 for hand in hands for x, y in hand
                        if abs(x - cx) < fw * 0.7 and abs(y - cy) < fh * 0.6)
        result[t] = {"faces": len(faces), "hands": len(hands), "near": near}

    json.dump(result, open("handface.json", "w"))
    flagged = sorted(t for t, v in result.items() if v["near"] > 0)
    clusters = []
    for t in flagged:
        if clusters and t - clusters[-1][1] <= 3:
            clusters[-1][1] = t
        else:
            clusters.append([t, t])
    print("frames", len(result), "flagged", len(flagged), "clusters", len(clusters))
    print(" ".join(f"{a // 60:02d}:{a % 60:02d}-{b // 60:02d}:{b % 60:02d}"
                   for a, b in clusters))
    for i in range(0, len(flagged), 240):
        grid([(f"camall/c_{t + 1:05d}.jpg", t) for t in flagged[i:i + 240]],
             f"flag_{i // 240}.jpg", 180, 107, 12)


def main(argv):
    if len(argv) < 2:
        sys.exit(__doc__.strip())
    os.chdir(argv[1])
    if "--sheet" in argv:
        for spec in argv[argv.index("--sheet") + 1:]:
            start, dur = (float(x) for x in spec.split(":"))
            sheet(start, dur, "zoom.jpg")
    elif "--range" in argv:
        start, end = (int(x) for x in argv[argv.index("--range") + 1].split(":"))
        items = frames_for(start, end)
        print("wrote cam_range.jpg", grid(items, "cam_range.jpg") if items else "no frames")
    else:
        detect()


if __name__ == "__main__":
    main(sys.argv)
