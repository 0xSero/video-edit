# video-edit

An agent skill + script kit that turns a raw screen/OBS/talk recording into a tight,
upload-ready video: transcript-driven cuts, dead-air and static-screen removal, redaction
of on-screen leaks, privacy cuts, a mastered audio track, and a verified render.

Built for one specific workflow — a 45–90 minute macOS screen recording (OBS, screen +
webcam) that has to become a 20–30 minute YouTube upload without leaking a hostname, an
API key, or five minutes of a cursor sitting still. Everything here runs locally.

> The surgical decisions (what to cut, which tangent is boring, where the opening is) are
> made by an agent reading the transcript. The scripts turn those decisions into a
> reproducible render with hard verification gates.

## What it actually fixes

| Problem in the raw recording | What the kit does |
| --- | --- |
| 30 minutes of "nothing happening" | frame-difference analysis flags visually static windows; that is not the same as silence |
| typing, fans, video playback | pause cuts are gated on RMS of the *enhanced* track, never on raw `silencedetect` |
| a tailnet hostname scrolling past in a terminal | whole-pane blur windows, then OCR of the *output* at 4 fps must find zero leaks |
| an off-limits moment (hand at the face, leaving the desk, face filling the frame) | Apple Vision hand-pose + face detection surfaces candidate clusters; a human/agent looks at the sheets before cutting |
| harsh mic, uneven levels | DeepFilterNet3 → EQ → de-esser → compressor → two-pass loudnorm to −16 LUFS |
| a choppy, black first 30 seconds | the opening is a deliberate decision, and the delivered file is re-checked frame by frame |

## Requirements

- macOS 14+ (Apple Silicon recommended). Three steps are macOS-only: Apple Vision OCR,
  Vision hand/face pose, and `h264_videotoolbox`.
- `ffmpeg` / `ffprobe` (6.0+; developed against 9.x), `python3` 3.10+
- `python3 -m pip install mlx-whisper deepfilternet pillow numpy soundfile pyobjc-framework-Vision pyobjc-framework-Quartz`
- Optional: `torchaudio` (only needed if the source audio is not 48 kHz)

Linux is not supported as-is: the OCR and pose passes need a replacement (Tesseract /
MediaPipe) and `h264_videotoolbox` becomes `libx264`. The EDL, audio and verification
logic are portable.

## Install

```bash
git clone git@github.com:0xSero/video-edit.git ~/.agents/skills/video-edit
# or, for Claude Code's skill dir:
ln -s ~/projects/video-edit ~/.claude/skills/video-edit
```

The skill is inert until an agent reads `SKILL.md`; the scripts work standalone too.

## Quick start

```bash
W=/tmp/edit && mkdir -p $W
bash scripts/prep.sh ~/Desktop/"2026-09-02 08-34-19.mov" $W   # audio, transcript, frames
python3 scripts/transcript_txt.py $W > $W/transcript.txt      # read this, decide keep ranges
python3 scripts/df_enhance.py $W/orig48.wav $W/df_enhanced.wav
$EDITOR $W/job.json                                            # keep/cuts/blur/boxes/chapters
python3 scripts/build_edl.py $W/job.json                       # -> segments.json
python3 scripts/render.py    $W/job.json                       # -> the delivered mp4
python3 scripts/verify_output.py $W/job.json                   # gate: exits 1 on any problem
python3 scripts/chapters.py  $W/job.json                       # timestamps in output time
```

`scripts/selftest.sh [workdir]` runs all of the above on a synthesized recording (`say`
speech + real silences) and asserts the gate in both directions — use it to check an
install, or to validate a change to any script:

```bash
bash scripts/selftest.sh /tmp/ved-selftest
```

The fixture carries fake PII: a `*.ts.net` hostname banner (0–8 s) covered by a blur
window, and a fake API key (12–16 s) left visible on purpose. The test asserts that the
source frame really OCRs the hostname, that the delivered file reports `leaks: 0` in the
blurred window, and that re-running the gate over the key's window **exits 1 with hits** —
a gate that cannot fail proves nothing.

## job.json

`scripts/job.example.json` is a complete example. Times are **source seconds**.

| key | meaning |
| --- | --- |
| `workdir` | the prep directory; every script chdirs here |
| `output` | output filename, relative to `workdir` |
| `keep` | `[[start, end], ...]` — the content decisions, in order, non-overlapping |
| `cuts` | `[[start, end], ...]` — privacy cuts subtracted from `keep` |
| `pause` | `{"max": 0.7, "keep": 0.4, "loud_db": -26}` — inside keeps, pauses longer than `max` are shortened to `keep`; only where enhanced RMS stays under `loud_db` |
| `blur` | `[{"t0", "t1"}]` — source windows to blur whole panes (scrolling terminal output) |
| `blur_regions` | `[[x, y, w, h]]` — panes to blur, in source pixels. **Required** whenever `blur` is set |
| `boxes` | `[{"x","y","w","h","t0","t1"}]` — static text to fill with solid colour |
| `audio_chain` | ffmpeg filter chain applied before loudnorm (default: highpass/EQ/de-esser/compressor) |
| `loudnorm` | `{"I": -16, "TP": -1.5, "LRA": 11}` |
| `render_groups` | parallel encode buckets (4 is a good default; bounds are cached) |
| `video_bitrate`, `encoder` | `14M`, `h264_videotoolbox` |
| `verify_windows` | extra `[[start, end]]` source ranges to OCR in the output |
| `chapters` | `[[source_seconds, "Title"]]` |

Frame rate and resolution are probed from the source, not configured — a mismatch there
silently retimes the whole video.

## The scripts

| script | role |
| --- | --- |
| `prep.sh` | extracts `orig48.wav` / `mono16.wav`, starts whisper, 0.5 fps frames, webcam crop, overview sheet. Never writes to the source |
| `overview.py` | contact sheet of sampled frames |
| `build_edl.py` | `keep` − `cuts`, pause tightening on the enhanced track, frame-grid snapping → `segments.json` |
| `render.py` | grouped parallel h264 encode (filter script per group, restartable), concat, audio cut with fades, two-pass loudnorm, mux with both audio tracks |
| `ocr_scan.py` | Apple Vision OCR + PII patterns (`run` / `scan` / `dense`) |
| `cam_flags.py` | Vision hand-pose and face rectangles over the webcam crop → candidate clusters |
| `df_enhance.py` | DeepFilterNet3 in 120 s chunks with crossfades, plus a torchaudio-import shim |
| `verify_output.py` | the gate: frame count, both audio tracks, OCR of the output around every redaction window |
| `chapters.py` | chapter list remapped to output time |
| `transcript_txt.py` | transcript as one timestamped line per segment |

### Restartability

`render.py` splits the timeline into `render_groups` buckets, caches the bucket bounds in
`group_bounds.json` and each group's filter script in `vf_<n>.txt`. A rerun skips any group
whose filter script and frame count are unchanged, so a failed 2-hour render resumes
instead of restarting. Use `--force` after changing the source, `--dry` to inspect the
ffmpeg commands.

### Why the verification gate exists

A render that "looks fine" is not evidence. `verify_output.py` independently re-derives
the expected frame count from `segments.json`, compares both audio track durations against
the video, and re-OCRs the finished file — because the one leak that ever shipped was a
hostname that appeared *between* two OCR samples of the source. Frames are what gets
uploaded, so frames are what get checked.

## Conventions the kit assumes

- OBS-style recordings, CFR 30/60 fps, screen + webcam-in-corner (crop defaults to the
  bottom-right 540×320 of a 1080p frame, scaled for other resolutions; override with
  `WEBCAM_CROP=w:h:x:y` or skip with `SKIP_CAM=1`).
- The talk is in `WHISPER_LANG` (default `en`), word timestamps on.
- The output keeps two audio tracks: enhanced (default) and the untouched mic, so the
  person who can actually hear the result can compare.
- 1080p unless told otherwise; 4K doubles render time for a screen recording.

## Troubleshooting

| symptom | cause / fix |
| --- | --- |
| `mlx_whisper: command not found` | `prep.sh` continues without a transcript; re-run whisper manually on `mono16.wav` |
| `torchaudio.backend` ImportError | `df_enhance.py` shims it; if you hit it elsewhere, import order matters |
| ffmpeg "Cannot allocate memory" | too many `between()` terms in one filter graph — lower `render_groups` |
| group frame mismatch | `render.py` refuses to concat; inspect `render_<n>.log`, usually a `-ss` before a non-keyframe with a broken decoder |
| leak still visible | scrolling text between samples: add the whole pane to `blur_regions` and widen the window 2 s each side |
| audio track shorter than video | a segment ran past EOF; `render.py` prints a padding warning |
| `cp` hits a TCC/sandbox prompt on `~/Downloads` | deliver with `/bin/cp` outside the sandbox |

## License

MIT — see [LICENSE](LICENSE).
