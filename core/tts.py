"""
core/tts.py — Text-to-Speech via the local Kokoro TTS server.

Extracted from clara2.py to decouple TTS from the monolithic script.
Sends text to a Kokoro TTS server and returns the audio as bytes.
"""

import re
import httpx

from core.services import TTS_HOST, TTS_PORT


def strip_markdown(text: str) -> str:
    """Clean markdown formatting for TTS playback.

    This is a *cleaner*, not a *transformer*. It removes visual markup
    (bold, headers, list markers, links) but leaves structural whitespace
    (newlines, paragraph breaks) intact so the chunker in ``speak()`` can
    decide sentence boundaries based on real punctuation, not artificial
    periods.
    """
    # Ordered replacements — run list markers first so bold inside them is
    # handled correctly.
    text = re.sub(r'^\s*\d+\.\s*\*+([^*]+)\*+:?', r'\1.', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*\d+\.\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*[-•]\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'\*+([^*]+)\*+', r'\1', text)
    text = re.sub(r'#+\s*', '', text)
    # NEWLINE → space (not period).  Paragraph breaks become double spaces
    # that the chunker will split on.
    text = re.sub(r'\n+', ' ', text)
    # Collapse multiple spaces to one.
    text = re.sub(r' {2,}', ' ', text)
    # Clean up period duplication that can appear after list-marker removal.
    text = re.sub(r'\.+', '.', text)
    text = re.sub(r'\.\s*\.', '.', text)
    text = re.sub(r'https?://\S+', '', text)
    return text.strip()


def synthesize(text: str, voice: str = "en_us_001") -> bytes | None:
    """Synthesize text to speech and return raw PCM/ WAV bytes.

    Parameters
    ----------
    text : str
        The text to synthesize.
    voice : str, optional
        Voice identifier (default: ``en_us_001``).

    Returns
    -------
    bytes | None
        Raw audio bytes on success, ``None`` on failure.
    """
    url = f"http://{TTS_HOST}:{TTS_PORT}/tts"
    payload = {
        "text": text,
        "voice": voice,
    }
    try:
        resp = httpx.post(url, json=payload, timeout=30.0)
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        print(f"[tts] synthesis failed: {e}")
        return None


def play(text: str, voice: str = "en_us_001") -> bool:
    """Synthesize *text* and play it through the default audio output.

    This is the high-level entry point used by the old code.  It
    synthesises the audio, then pipes it to ``aplay`` (Linux) or
    ``afplay`` (macOS) via subprocess.

    Returns ``True`` on success, ``False`` on failure.
    """
    import subprocess
    audio = synthesize(text, voice)
    if audio is None:
        return False

    # Try common Linux players first, fall back to Python library
    for player in ("pw-play", "aplay", "afplay"):
        try:
            args = [player, "--raw", "--format=s16", "-"] if player == "pw-play" else [player]
            proc = subprocess.run(
                args,
                input=audio,
                timeout=30,
                check=True,
            )
            return True
        except FileNotFoundError:
            continue
        except subprocess.CalledProcessError as e:
            print(f"[tts] player {player} failed: {e}")
            continue

    # Last resort: use pygame (must be installed)
    try:
        import pygame.mixer
        pygame.mixer.init(frequency=24000, size=-16, channels=1)
        import io
        sound = pygame.mixer.Sound(io.BytesIO(audio))
        pygame.mixer.Channel(0).play(sound)
        while pygame.mixer.get_busy():
            pygame.time.Clock().tick(10)
        return True
    except ImportError:
        print("[tts] pygame not available for audio playback")
        return False
    except Exception as e:
        print(f"[tts] pygame playback failed: {e}")
        return False