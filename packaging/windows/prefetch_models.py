"""Download the offline speech-to-text models into one folder.

Produces the layout ``pillow_assistant.core.asr`` looks for through
``PILLOW_MODELS_DIR`` (the bundle's ``models/`` folder)::

    models/SenseVoiceSmall/          iic/SenseVoiceSmall          (ModelScope)
    models/fsmn-vad/                 iic/speech_fsmn_vad_zh-cn-16k-common-pytorch
    models/faster-whisper-small/     Systran/faster-whisper-small (Hugging Face)

Usage:  pip install modelscope huggingface_hub
        python prefetch_models.py <output-dir> [--whisper small]
"""

from __future__ import annotations

import argparse
from pathlib import Path

MODELSCOPE = {
    "SenseVoiceSmall": "iic/SenseVoiceSmall",
    "fsmn-vad": "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("out", type=Path)
    parser.add_argument("--whisper", default="small", help="faster-whisper size; '' to skip")
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)

    from modelscope import snapshot_download as ms_download
    for folder, model_id in MODELSCOPE.items():
        print(f"ModelScope {model_id} -> {out / folder}", flush=True)
        ms_download(model_id, local_dir=str(out / folder))

    if args.whisper:
        from huggingface_hub import snapshot_download as hf_download
        repo = f"Systran/faster-whisper-{args.whisper}"
        print(f"Hugging Face {repo} -> {out / ('faster-whisper-' + args.whisper)}", flush=True)
        hf_download(repo, local_dir=str(out / f"faster-whisper-{args.whisper}"))

    for cache in out.rglob(".cache"):  # hub bookkeeping, not needed offline
        if cache.is_dir():
            import shutil
            shutil.rmtree(cache, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
