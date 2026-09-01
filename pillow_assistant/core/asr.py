"""Local speech-to-text (R1+), pluggable backends.

Backends (picked automatically, best first):
  * sensevoice — SenseVoice-Small via funasr. Markedly better Chinese/Cantonese
    than Whisper, non-autoregressive (≈15x faster), great for short utterances.
    Install:  pip install funasr  (first use downloads ~1GB from ModelScope)
  * whisper   — faster-whisper fallback.
    Install:  pip install faster-whisper

Tuning via environment variables:
  * PILLOW_ASR_BACKEND - force "sensevoice" or "whisper" (default: auto)
  * PILLOW_ASR_MODEL   - whisper size tiny/base/small/medium (default small);
                         ignored by sensevoice
  * PILLOW_ASR_LANG    - force language, e.g. zh / en (default: auto-detect)

Graceful: ``available()`` is False when no backend is installed and callers
fall back to typed input. Models load lazily and are cached; the first call may
download weights, so always run ``transcribe`` off the UI thread.
"""

from __future__ import annotations

import os
from typing import Optional


from pillow_assistant.capabilities.prompt_registry import get_prompt_registry
BACKEND = os.environ.get("PILLOW_ASR_BACKEND", "").strip().lower() or None
DEFAULT_MODEL = os.environ.get("PILLOW_ASR_MODEL", "small")
DEFAULT_LANG = os.environ.get("PILLOW_ASR_LANG") or None  # None = auto-detect

_sv_model = None       # cached SenseVoice model
_wh_model = None       # cached faster-whisper model
_wh_size = None


def _have(module: str) -> bool:
    try:
        __import__(module)
        return True
    except Exception:
        return False


_backend_cache: Optional[str] = None  # "" = probed, none installed


def _roles_asr() -> dict:
    """Agent-assigned ASR preference from ~/.pillow/model_roles.json."""
    try:
        from pillow_assistant.core.model_roles import load_roles
        v = load_roles().get("asr")
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def reset_cache() -> None:
    """Forget cached backend/models (after the asr role is re-assigned)."""
    global _backend_cache, _sv_model, _wh_model, _wh_size
    _backend_cache = None
    _sv_model = None
    _wh_model = None
    _wh_size = None


def backend() -> Optional[str]:
    """The backend that will be used, or None if none is installed.

    Preference: PILLOW_ASR_BACKEND env > assigned asr role > auto. A preferred
    backend whose package is missing degrades to auto. The probe imports heavy
    packages (funasr pulls in torch — seconds), so the result is cached; call
    ``warmup()`` from a background thread at startup so the first UI-thread
    call never blocks.
    """
    global _backend_cache
    if _backend_cache is not None:
        return _backend_cache or None
    pref = BACKEND or str(_roles_asr().get("backend") or "").lower()
    if pref == "sensevoice" and _have("funasr"):
        result = "sensevoice"
    elif pref == "whisper" and _have("faster_whisper"):
        result = "whisper"
    elif _have("funasr"):
        result = "sensevoice"
    elif _have("faster_whisper"):
        result = "whisper"
    else:
        result = ""
    _backend_cache = result
    return result or None


def warmup() -> None:
    """Probe (and cache) the backend choice; run me off the UI thread."""
    backend()


def available() -> bool:
    return backend() is not None


# -- SenseVoice (funasr) -----------------------------------------------------
def _sensevoice_transcribe(wav_path: str, language: Optional[str]) -> str:
    global _sv_model
    if _sv_model is None:
        from funasr import AutoModel
        _sv_model = AutoModel(
            model="iic/SenseVoiceSmall",
            vad_model="fsmn-vad",
            vad_kwargs={"max_single_segment_time": 30000},
            device="cpu",
            disable_update=True,
        )
    res = _sv_model.generate(
        input=wav_path,
        language=(language or DEFAULT_LANG or "auto"),
        use_itn=True,            # numbers/punctuation normalization
        merge_vad=True,
    )
    text = res[0]["text"] if res else ""
    try:  # strip emotion/event tags like <|zh|><|NEUTRAL|>…
        from funasr.utils.postprocess_utils import rich_transcription_postprocess
        text = rich_transcription_postprocess(text)
    except Exception:
        import re
        text = re.sub(r"<\|[^|]*\|>", "", text)
    return text.strip()


# -- faster-whisper ----------------------------------------------------------
def _whisper_size() -> str:
    return os.environ.get("PILLOW_ASR_MODEL") or str(_roles_asr().get("model") or "") or "small"


def _whisper_model(size: Optional[str] = None):
    global _wh_model, _wh_size
    size = size or _whisper_size()
    if _wh_model is None or _wh_size != size:
        from faster_whisper import WhisperModel
        _wh_model = WhisperModel(size, device="cpu", compute_type="int8")
        _wh_size = size
    return _wh_model


def _whisper_transcribe(wav_path: str, language: Optional[str]) -> str:
    model = _whisper_model()
    segments, _info = model.transcribe(
        wav_path,
        language=language or DEFAULT_LANG,
        beam_size=5,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 300},
        # Mandarin punctuation hint — only when the app language is Chinese,
        # otherwise it would bias non-Chinese speech toward zh.
        initial_prompt=(get_prompt_registry().render("asr.whisper_initial", language="zh") if _lang_is_zh() else None),
        condition_on_previous_text=False,
    )
    return "".join(seg.text for seg in segments).strip()


def _lang_is_zh() -> bool:
    try:
        from pillow_assistant.core.i18n import LANG
        return LANG == "zh"
    except Exception:
        return True


def transcribe(wav_path: str, language: Optional[str] = None) -> str:
    """Transcribe a WAV file to text. Raises if no ASR backend is installed."""
    b = backend()
    if b == "sensevoice":
        return _sensevoice_transcribe(wav_path, language)
    if b == "whisper":
        return _whisper_transcribe(wav_path, language)
    from pillow_assistant.core.i18n import t
    raise RuntimeError(t("asr.no_backend"))
