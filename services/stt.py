"""Распознавание речи через faster-whisper (модель загружается лениво)."""
import asyncio
import logging

from config import WHISPER_MODEL, WHISPER_DEVICE, WHISPER_COMPUTE

log = logging.getLogger(__name__)

_model = None


def _get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        log.info("Загружаю модель Whisper '%s' (device=%s, compute=%s)...",
                 WHISPER_MODEL, WHISPER_DEVICE, WHISPER_COMPUTE)
        _model = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE)
        log.info("Модель Whisper загружена.")
    return _model


def _transcribe_sync(path: str, language: str | None = None) -> str:
    model = _get_model()
    segments, _info = model.transcribe(path, language=language, vad_filter=True)
    return " ".join(seg.text.strip() for seg in segments).strip()


async def transcribe(path: str, language: str | None = None) -> str:
    """Асинхронно распознать аудиофайл (в отдельном потоке, чтобы не блокировать event loop)."""
    return await asyncio.to_thread(_transcribe_sync, path, language)
