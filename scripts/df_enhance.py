#!/usr/bin/env python3
"""df_enhance.py <in.wav> <out.wav> - DeepFilterNet3 speech enhancement, mono 48 kHz.

Two things make this usable on hour-long recordings:

  * DeepFilterNet 0.5.6 imports torchaudio.backend.common, which current torchaudio no
    longer ships, so stub that module before importing df.
  * The whole file is never in memory: 120 s chunks with 2 s linear crossfades, written
    as one PCM16 wav at the model sample rate.
"""
import sys
import types

import numpy as np
import soundfile as sf
import torch

for name in ("torchaudio.backend", "torchaudio.backend.common"):
    if name not in sys.modules:
        module = types.ModuleType(name)
        sys.modules[name] = module
if not hasattr(sys.modules["torchaudio.backend.common"], "AudioMetaData"):
    class AudioMetaData:
        pass

    sys.modules["torchaudio.backend.common"].AudioMetaData = AudioMetaData

from df.enhance import enhance, init_df  # noqa: E402

CHUNK, OVERLAP = 120, 2  # seconds


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__.strip())
    src, dst = sys.argv[1], sys.argv[2]
    model, state, _ = init_df()
    sr_model = state.sr()

    audio, sr = sf.read(src, dtype="float32")
    if audio.ndim == 2:
        audio = audio.mean(1)
    if sr != sr_model:
        import torchaudio
        audio = torchaudio.functional.resample(
            torch.from_numpy(audio), sr, sr_model).numpy()
        sr = sr_model

    total = len(audio)
    chunk, overlap = CHUNK * sr, min(OVERLAP * sr, total // 4)
    out = np.zeros(total, dtype=np.float32)
    weight = np.zeros(total, dtype=np.float32)
    start = 0
    while start < total:
        end = min(total, start + chunk)
        with torch.no_grad():
            enhanced = enhance(model, state, torch.from_numpy(
                audio[start:end]).unsqueeze(0)).squeeze(0).cpu().numpy()
        length = end - start
        enhanced = enhanced[:length]
        w = np.ones(length, dtype=np.float32)
        if start > 0:
            w[:overlap] = np.linspace(0, 1, overlap)
        if end < total:
            w[-overlap:] = np.linspace(1, 0, overlap)
        out[start:end] += enhanced * w
        weight[start:end] += w
        print(f"{end / sr / 60:.1f} min", flush=True)
        start = end - overlap if end < total else total

    out /= np.maximum(weight, 1e-6)
    sf.write(dst, out, sr, subtype="PCM_16")
    # sanity: enhancement must stay correlated with the input, not replace it
    i = min(total // 2, max(0, total - 20 * sr))
    j = min(total, i + 20 * sr)
    corr = float(np.corrcoef(audio[i:j], out[i:j])[0, 1]) if j > i + 10 else float("nan")
    print(f"done {dst}; correlation with input: {corr:.3f} (expect > 0.95)")


if __name__ == "__main__":
    main()
