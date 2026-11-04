#!/usr/bin/env bash
# prep.sh <source> <workdir>
#
# One-time extraction for an edit job. Never modifies the source. Writes into <workdir>:
#   src.mov           symlink to the untouched source
#   orig48.wav        original mic, 48 kHz stereo   (becomes audio track 2)
#   mono16.wav        16 kHz mono copy for whisper
#   orig48_mono.wav   mono copy for level diagnostics
#   transcript.json   word-timestamped transcript (whisper runs in the background)
#   silences_raw.txt  raw silencedetect output - DIAGNOSTIC ONLY, cuts are gated on the
#                     enhanced track inside build_edl.py
#   frames/           one frame per 2 s (OCR / leak scan)
#   camall/           webcam corner crop at 1 fps (SKIP_CAM=1 to skip)
#   webcam_crop.txt   the crop geometry cam_flags.py reuses
#   sheet_overview.jpg 4x6 contact sheet of the whole recording
#
# Env: SKIP_CAM=1, WEBCAM_CROP=w:h:x:y, WHISPER_LANG=en,
#      WHISPER_MODEL=mlx-community/whisper-large-v3-turbo
set -euo pipefail
if [ $# -ne 2 ]; then
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
fi

SRC=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
W=$2
SK=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$W/frames" "$W/camall"
cd "$W"
ln -sf "$SRC" src.mov

echo "== source"
ffprobe -v error -show_entries format=duration \
  -show_entries stream=codec_type,width,height,r_frame_rate,channels -of compact src.mov

SIZE=$(ffprobe -v error -select_streams v:0 -show_entries stream=width,height \
  -of default=nw=1:nk=1 src.mov | tr '\n' ' ')
read -r VW VH <<< "$SIZE"
CW=$((VW * 540 / 1920)); CH=$((VH * 320 / 1080))
CROP=${WEBCAM_CROP:-$CW:$CH:$((VW - CW)):$((VH - CH))}
printf '%s\n' "$CROP" > webcam_crop.txt
echo "webcam crop $CROP (source ${VW}x${VH})"

echo "== audio"
ffmpeg -hide_banner -loglevel error -y -i src.mov \
  -vn -ac 2 -ar 48000 -c:a pcm_s16le orig48.wav \
  -vn -ac 1 -ar 16000 -c:a pcm_s16le mono16.wav
ffmpeg -hide_banner -loglevel error -y -i orig48.wav -ac 1 orig48_mono.wav

echo "== transcript"
WPID=
if command -v mlx_whisper >/dev/null 2>&1; then
  nohup mlx_whisper mono16.wav \
    --model "${WHISPER_MODEL:-mlx-community/whisper-large-v3-turbo}" \
    --language "${WHISPER_LANG:-en}" --word-timestamps True \
    --output-format json --output-dir . --output-name transcript \
    > whisper.log 2>&1 &
  WPID=$!
  echo "whisper pid $WPID -> transcript.json (tail whisper.log)"
else
  echo "!! mlx_whisper not found: pip install mlx-whisper (WebKit-free, Apple Silicon)" >&2
fi

echo "== silence diagnostic (raw mic, not used for cuts)"
ffmpeg -hide_banner -i mono16.wav -af silencedetect=noise=-35dB:d=0.7 -f null - 2>&1 \
  | grep -E 'silence_(start|end)' > silences_raw.txt || true

echo "== frames"
ffmpeg -hide_banner -loglevel error -y -i src.mov -vf fps=1/2 -q:v 3 frames/f_%05d.jpg &
P1=$!
if [ "${SKIP_CAM:-0}" = "1" ]; then
  wait "$P1"
else
  ffmpeg -hide_banner -loglevel error -y -i src.mov -vf "fps=1,crop=$CROP" -q:v 4 camall/c_%05d.jpg &
  P2=$!
  wait "$P1" "$P2"
fi

python3 "$SK/overview.py" frames sheet_overview.jpg 2 4
echo "prep done${WPID:+; whisper still running (pid $WPID), tail whisper.log}"
