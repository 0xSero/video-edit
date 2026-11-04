#!/usr/bin/env bash
# selftest.sh [workdir]
#
# End-to-end smoke test with no source footage needed: synthesizes a 720p30 recording
# from `say` speech separated by real silence, then runs prep -> enhance -> EDL ->
# render -> verify -> chapters and asserts the delivered file passes the gate.
#
# Needs: ffmpeg, python3, mlx_whisper, DeepFilterNet, Pillow, soundfile, numpy.
set -euo pipefail

SK=$(cd "$(dirname "$0")" && pwd)
W=${1:-$(mktemp -d)/smoke}
mkdir -p "$W/parts"
cd "$W"
echo "== workdir $W"

say -o parts/a.wav --data-format=LEI16@48000 \
  "Smoke test one. This sentence exists so the pipeline has speech to keep."
say -o parts/b.wav --data-format=LEI16@48000 \
  "Smoke test two. Long pauses around this line should be tightened by the editor."
say -o parts/c.wav --data-format=LEI16@48000 \
  "Smoke test three. If you can read this line the render and mux worked."
ffmpeg -hide_banner -loglevel error -y -f lavfi -i anullsrc=r=48000:cl=mono \
  -t 4 -c:a pcm_s16le parts/gap.wav
printf "file '%s'\n" "$W/parts/a.wav" "$W/parts/gap.wav" "$W/parts/b.wav" \
  "$W/parts/gap.wav" "$W/parts/c.wav" > parts/list.txt
ffmpeg -hide_banner -loglevel error -y -f concat -safe 0 -i parts/list.txt -c copy talk.wav

DUR=$(ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 talk.wav)
echo "== source audio ${DUR}s; muxing 1280x720@30 video with fake PII"

# Fake PII, so the redaction path and the delivery gate are both exercised:
#  - a hostname banner (0-8 s) that job.blur must cover
#  - a fake API key (12-16 s) that stays visible and MUST trip verify_output.py
python3 - <<'EOF'
from PIL import Image, ImageDraw, ImageFont
W, H = 1280, 720
font = None
for path in ("/System/Library/Fonts/SFNSMono.ttf",
             "/System/Library/Fonts/Supplemental/Menlo.ttc",
             "/Library/Fonts/Menlo.ttc"):
    try:
        font = ImageFont.truetype(path, 22)
        break
    except OSError:
        continue
font = font or ImageFont.load_default()

def layer(text, xy):
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    width = draw.textlength(text, font=font)
    draw.rectangle([xy[0] - 8, xy[1] - 6, xy[0] + width + 8, xy[1] + 30],
                   fill=(16, 16, 20, 240))
    draw.text(xy, text, font=font, fill=(235, 235, 235, 255))
    return img

layer("ssh deploy@198.51.100.23 demo-node.example-tailnet.ts.net", (40, 600)).save("pl_host.png")
layer("export HF_TOKEN=hf_FAKEfixturekey000000000000", (40, 660)).save("pl_key.png")
EOF

ffmpeg -hide_banner -loglevel error -y \
  -f lavfi -i "testsrc2=size=1280x720:rate=30:duration=$DUR" \
  -loop 1 -i pl_host.png -loop 1 -i pl_key.png -i talk.wav \
  -filter_complex "[0:v][1:v]overlay=0:0:enable='between(t,0,8)'[v0];\
[v0][2:v]overlay=0:0:enable='between(t,12,16)'[v]" \
  -map "[v]" -map 3:a -t "$DUR" -c:v h264_videotoolbox -b:v 6M -c:a aac source.mov

echo "== prep (whisper runs in the background)"
bash "$SK/prep.sh" "$W/source.mov" "$W/edit"

# If the fixture text were invisible, every later "no leaks" would be meaningless.
mkdir -p srcframe
ffmpeg -hide_banner -loglevel error -y -ss 2 -i source.mov -frames:v 1 \
  -start_number 4 "srcframe/d_%06d.jpg"
python3 "$SK/ocr_scan.py" run srcframe src_ocr.json --shards 1 > /dev/null
python3 "$SK/ocr_scan.py" scan src_ocr.json > src_scan.txt 2>&1 || true
if ! grep -q 'example-tailnet\.ts\.net' src_scan.txt; then
  echo "FAIL: the fake hostname is not readable in the source frame"
  cat src_scan.txt
  exit 1
fi
echo "fixture sanity: source frame OCR sees the fake hostname"

if command -v mlx_whisper >/dev/null 2>&1; then
  for _ in $(seq 1 120); do
    [ -s edit/transcript.json ] && python3 -c "import json,sys;json.load(open('edit/transcript.json'))" 2>/dev/null && break
    sleep 2
  done
  echo "== transcript: $(python3 "$SK/transcript_txt.py" edit | head -1 || echo 'not ready')"
else
  echo "== mlx_whisper missing: skipping transcript (silence detection still applies)"
fi

echo "== DeepFilterNet"
python3 "$SK/df_enhance.py" edit/orig48.wav edit/df_enhanced.wav

cat > edit/job.json <<EOF
{
  "workdir": "$W/edit",
  "output": "Smoke - edit.mp4",
  "target_minutes": 0.5,
  "keep": [[0, $DUR]],
  "cuts": [[6.0, 7.0]],
  "blur": [{"t0": 0.0, "t1": 8.0}],
  "blur_regions": [[10, 585, 900, 60]],
  "boxes": [{"x": 640, "y": 480, "w": 300, "h": 60, "t0": 3.0, "t1": 4.0}],
  "render_groups": 2,
  "chapters": [[0, "Opening"], [8, "Middle"], [18, "Close"]]
}
EOF

echo "== EDL"
python3 "$SK/build_edl.py" edit/job.json
echo "== render"
python3 "$SK/render.py" edit/job.json
echo "== verify (delivery gate: the blurred hostname must be gone, the fake key is outside)"
python3 "$SK/verify_output.py" edit/job.json
echo "== chapters"
python3 "$SK/chapters.py" edit/job.json

# Negative control: the same file re-checked with the fake key's window included.
# If the gate cannot fail, it proves nothing - so this MUST exit 1 and report a leak.
python3 - <<'EOF'
import json
job = json.load(open("edit/job.json"))
job["verify_windows"] = [[12, 16]]
json.dump(job, open("edit/job.leak.json", "w"))
EOF
set +e
python3 "$SK/verify_output.py" edit/job.leak.json > control.log 2>&1
RC=$?
set -e
if [ "$RC" -ne 1 ]; then
  echo "FAIL: negative control exited $RC, expected 1"; cat control.log; exit 1
fi
if ! grep -q 'leaks: [1-9]' control.log; then
  echo "FAIL: negative control reported no leak"; cat control.log; exit 1
fi
echo "negative control: rc=1, $(grep -o 'leaks: [0-9]*' control.log | head -1) (expected > 0)"

OUT="edit/Smoke - edit.mp4"
[ -s "$OUT" ] || { echo "FAIL: no output"; exit 1; }
OUTDUR=$(ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$OUT")
python3 - "$DUR" "$OUTDUR" <<'EOF'
import sys
src, out = float(sys.argv[1]), float(sys.argv[2])
print(f"source {src:.2f}s -> output {out:.2f}s (-{src - out:.2f}s)")
sys.exit(0 if 0 < out < src else 1)
EOF
echo "PASS: $OUT"
