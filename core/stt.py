"""
core/stt.py — STTClient: CPU-only speech-to-text via faster-whisper.
"""

import gc
import numpy as np
from faster_whisper import WhisperModel


class STTClient:
    """Wraps faster-whisper for CPU transcription. int8 quantization keeps
    CPU inference reasonably fast since the GPU is fully committed to the
    LLM/TTS/image/video pipelines and this has to share cores with the
    wake-word listener."""

    def __init__(self, model_size: str = "small", device: str = "cpu",
                 compute_type: str = "int8", cpu_threads: int = 2):
        """cpu_threads defaults to 2, not faster-whisper's own default
        (0 = use all detected cores). Whisper only runs in short bursts
        (per-utterance), but it now shares this CPU with a continuous
        160ms-cadence openWakeWord inference loop that's always running,
        plus whatever's still settling from Gemma/ComfyUI's own startup
        loads. Letting Whisper grab every core for its burst starves the
        wake-word loop right when it happens to overlap, which was part
        of what turned startup into total resource contention. 2 is a
        starting point — raise it if transcription latency matters more
        than headroom for you in practice."""
        print(f"[stt] Loading faster-whisper '{model_size}' ({device}/{compute_type}, cpu_threads={cpu_threads})...")
        self.model = WhisperModel(model_size, device=device, compute_type=compute_type,
                                   cpu_threads=cpu_threads)
        print("[stt] Whisper ready.")

    def transcribe(self, audio: np.ndarray, sample_rate: int = 16000) -> str:
        """audio must be float32 mono in [-1, 1] at 16kHz (faster-whisper's
        expected input format — resample before calling if your capture
        rate differs)."""
        if audio is None or len(audio) == 0:
            return ""
        segments, _info = self.model.transcribe(
            audio,
            language="en",
            vad_filter=True,          # trims leading/trailing silence, helps accuracy
            beam_size=5,
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        return text

    def shutdown(self) -> None:
        """Release the faster-whisper model and free GPU/CPU resources.
        
        This must be called during application shutdown to properly release
        the model's internal resources (CUDA contexts, memory mappings, etc.)
        so that a subsequent restart doesn't hang trying to load the same
        model while previous resources are still held.
        """
        if hasattr(self, 'model') and self.model is not None:
            del self.model
            self.model = None
            gc.collect()
