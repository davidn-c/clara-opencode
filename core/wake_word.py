"""
core/wake_word.py — WakeWordListener: always-on CPU wake-word detection
via openWakeWord, followed by silence-terminated recording for STT.

Capture uses `parecord` via subprocess instead of sounddevice/PortAudio -
PortAudio hung indefinitely against the previous Bluetooth mic source, so
capture is done externally the same way speak() shells out to pw-play for
output. Now pointed at a USB mic (Blue Snowball), which captures stereo,
so each chunk is downmixed to mono before being handed to openWakeWord /
faster-whisper (both expect mono).
"""
import fcntl
import subprocess
import threading
import time
import os
import shutil
import numpy as np
import openwakeword
from openwakeword.model import Model as OWWModel
from scipy.signal import resample

_MIC_AVAILABLE = shutil.which("parecord") is not None

MIC_DEVICE = "alsa_input.usb-BLUE_MICROPHONE_Blue_Snowball_201305-00.analog-stereo"
CAPTURE_RATE = 48000       # device's native rate
CAPTURE_CHANNELS = 2       # Snowball captures stereo; downmixed to mono below
TARGET_RATE = 16000        # rate expected by openWakeWord / faster-whisper
CHUNK_MS = 160
CHUNK_BYTES = int(CAPTURE_RATE * CHUNK_MS / 1000) * 2 * CAPTURE_CHANNELS  # 2 bytes/sample * channels
WAKE_COOLDOWN_S = 3.0

# Silence-detection tuning for ending an utterance automatically —
# no button press to signal "done talking" like push-to-talk had.
SILENCE_RMS_THRESHOLD = 0.003
SILENCE_DURATION_S = 1.0
MAX_UTTERANCE_S = 15.0

# Set to True to print a running score/RMS line while debugging trigger
# sensitivity; noisy in normal use, so off by default once things work.
DEBUG = True
DEBUG_EVERY_N_CHUNKS = 20


def _bytes_to_mono_float(raw: bytes) -> np.ndarray:
    """Converts raw s16le bytes (possibly multi-channel) into a mono
    float32 array in [-1, 1]."""
    audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if CAPTURE_CHANNELS == 2:
        audio = audio.reshape(-1, 2).mean(axis=1)
    return audio

def _bytes_available(stream) -> int:
    """Returns how many bytes are currently queued in the pipe without
    blocking, so _loop() can detect and skip a growing backlog instead
    of always processing one stale chunk behind."""
    fd = stream.fileno()
    import struct
    import termios
    buf = fcntl.ioctl(fd, termios.FIONREAD, struct.pack('I', 0))
    return struct.unpack('I', buf)[0]

def _drain_pending(proc, chunk_bytes):
    """Non-blocking flush of any bytes already queued in the pipe —
    backlog built up while _loop() was busy with predict()/resample()
    calls that took longer than real-time. Without this, _handle_wake()
    starts by replaying stale audio instead of what's happening now."""
    fd = proc.stdout.fileno()
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    drained = 0
    try:
        while True:
            chunk = proc.stdout.read(chunk_bytes)
            if not chunk:
                break
            drained += len(chunk)
    except BlockingIOError:
        pass
    finally:
        fcntl.fcntl(fd, fcntl.F_SETFL, flags)
    if drained:
        print(f"[wakeword] drained {drained} bytes of backlog before recording")

def _resample(chunk: np.ndarray, orig_rate: int, target_rate: int) -> np.ndarray:
    if orig_rate == target_rate:
        return chunk.astype(np.float32)
    target_len = int(len(chunk) * target_rate / orig_rate)
    return resample(chunk, target_len).astype(np.float32)


class WakeWordListener:
    def __init__(self, on_wake, on_utterance, model_name: str = "hey_clara_v0.1",
                     threshold: float = 0.5, wake_word_enabled: bool = True):
            """
            on_wake: called (no args) the instant the wake word fires.
            on_utterance: called with the recorded float32 numpy audio buffer
                     (at TARGET_RATE / 16000Hz, mono) once silence ends the
                     recording.
            wake_word_enabled: when False, openWakeWord is never loaded and
                     _loop() never runs inference on captured audio — capture
                     and the manual trigger_now()/_handle_wake() path (used by
                     the "Talk to Clara" button) are unaffected either way.
                     Exposed as a GUI checkbox so wake-word detection can be
                     re-enabled later for testing without a code change.
            """
            self.model_name = model_name
            self.wake_word_enabled = wake_word_enabled
            if self.wake_word_enabled:
                oww_dir = os.path.dirname(openwakeword.__file__)
                model_path = os.path.join(oww_dir, "resources", "models", f"{model_name}.onnx")
                self.oww_model = OWWModel(wakeword_model_paths=[model_path])
            else:
                self.oww_model = None
            self.threshold = threshold
            self.on_wake = on_wake
            self.on_utterance = on_utterance

            self._stop = threading.Event()
            self._muted = threading.Event()
            self._manual_trigger = threading.Event()
            # _recording is True for the entire span _handle_wake() owns the
            # pipe (set/cleared there, in a try/finally so it can't get stuck
            # set on an exception). trigger_now() reads it to decide whether a
            # button press should start a new recording or stop the one in
            # progress. _manual_stop is the "stop" half of that toggle.
            self._recording = threading.Event()
            self._manual_stop = threading.Event()
            self._thread = None
            self._proc = None
        
    # ── Lifecycle ────────────────────────────────────────────

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._proc is not None:
            self._proc.terminate()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def mute(self):
        """Call when TTS starts, so Clara doesn't hear/react to herself."""
        self._muted.set()

    def unmute(self):
        """Call when TTS finishes."""
        self._muted.clear()

    def set_enabled(self, enabled: bool) -> None:
            """Toggle wake-word detection at runtime — e.g. from a GUI
            checkbox. Lazily loads the openWakeWord model on first enable if
            it wasn't loaded at construction time (i.e. it started disabled).
            Safe to call while _loop() is running: it only ever reads
            self.wake_word_enabled/self.oww_model at the top of each chunk
            iteration, never mid-inference."""
            if enabled and self.oww_model is None:
                oww_dir = os.path.dirname(openwakeword.__file__)
                model_path = os.path.join(oww_dir, "resources", "models", f"{self.model_name}.onnx")
                self.oww_model = OWWModel(wakeword_model_paths=[model_path])
            self.wake_word_enabled = enabled

    def trigger_now(self):
        """Toggle: if no recording is currently in progress, start one
        immediately, bypassing wake-word detection — e.g. from a GUI 'Talk
        to Clara' button. If a recording IS already in progress — whether
        it was started by a real wake word or by a previous press of this
        same button — end it right now instead of waiting for the silence
        timer or the MAX_UTTERANCE_S cap.

        Either way this just sets a flag; _loop() / _handle_wake() (running
        on their own thread) pick it up on their next chunk read, so the
        recording is still driven entirely from that one thread. That
        matters because _handle_wake() reads directly from the parecord
        pipe — if this method read from that pipe on a second thread
        concurrently with _loop(), the two reads would race for the same
        bytes and corrupt whatever gets captured. Routing through flags
        keeps everything single-threaded against the pipe, same as a real
        wake-word trigger. If TTS is currently playing (muted) and no
        recording is in progress, the start flag stays set and fires as
        soon as unmuted, rather than being lost."""
        if self._recording.is_set():
            self._manual_stop.set()
        else:
            self._manual_trigger.set()

    # ── Main loop ────────────────────────────────────────────
    def _start_capture(self):
        return subprocess.Popen(
            ["parecord", f"--device={MIC_DEVICE}", "--raw",
             "--rate", str(CAPTURE_RATE), "--channels", str(CAPTURE_CHANNELS),
             "--format=s16le"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )

    def _loop(self):
        if not _MIC_AVAILABLE:
            print("[wakeword] parecord not found — audio capture disabled")
            return
        try:
            self._proc = self._start_capture()
        except FileNotFoundError:
            print("[wakeword] parecord not found — audio capture disabled")
            return

        n = 0

        while not self._stop.is_set():
            raw = self._proc.stdout.read(CHUNK_BYTES)
            if not raw:
                print("[wakeword] parecord returned no data, stopping loop")
                break

            # If more data is already sitting in the pipe than we just
            # read, we're falling behind real-time — skip ahead to the
            # newest chunk instead of processing a growing backlog.
            available = _bytes_available(self._proc.stdout)
            if available >= CHUNK_BYTES:
                skip = (available // CHUNK_BYTES) * CHUNK_BYTES
                self._proc.stdout.read(skip)

            chunk = _bytes_to_mono_float(raw)

            if self._muted.is_set():
                continue

            if self._manual_trigger.is_set():
                self._manual_trigger.clear()
                print("[wakeword] manual trigger (Talk button) — starting utterance recording")
                self._fire_trigger()
                continue

            if not self.wake_word_enabled:
                continue

            chunk_16k = _resample(chunk, CAPTURE_RATE, TARGET_RATE)
            chunk_16k_int16 = np.clip(chunk_16k * 32768.0, -32768, 32767).astype(np.int16)
            try:
                scores = self.oww_model.predict(chunk_16k_int16)
            except Exception as e:
                print(f"[wakeword] predict failed: {e}")
                continue

            score = scores.get(self.model_name, 0.0)
            n += 1
            if DEBUG and n % DEBUG_EVERY_N_CHUNKS == 0:
                print(f"[wakeword] DEBUG: scores_dict={scores}, raw_rms={np.sqrt(np.mean(chunk**2)):.4f}")

            if score >= self.threshold:
                print(f"[wakeword] triggered (score={score:.2f})")
                self._fire_trigger()

        if self._proc is not None:
            self._proc.terminate()
                

    def _fire_trigger(self) -> None:
            """Shared by both the wake-word path and the manual-trigger path:
            record the utterance, reset the model's rolling buffer (if wake
            word is enabled and loaded), wait out the cooldown, then drain
            whatever backlog built up during both."""
            self._handle_wake()
            if self.wake_word_enabled and hasattr(self.oww_model, "reset"):
                self.oww_model.reset()
            time.sleep(WAKE_COOLDOWN_S)
            # Backlog can still build during the ~3s _handle_wake()
            # window (it owns the pipe read, so the skip-ahead logic
            # above doesn't run during that time) and during the
            # cooldown sleep itself — drain both away before we go
            # back to listening for the next wake word.
            _drain_pending(self._proc, CHUNK_BYTES)

    def _handle_wake(self) -> None:
        # _recording spans the whole method (set here, cleared in the
        # finally below) so trigger_now() can reliably tell "is a
        # recording in progress right now" regardless of how it started or
        # how this method exits (normal end, muted-discard return, or the
        # MAX_UTTERANCE_S cap). try/finally guarantees it can't get stuck
        # set if anything above raises.
        self._recording.set()
        try:
            # Defensively clear any stale manual-stop flag from a prior
            # recording before this one starts. Without this, a button
            # press that lands in the narrow window right as a previous
            # recording is finishing (is_recording() briefly still True in
            # trigger_now()'s check) would leave _manual_stop set, and the
            # very next recording — even a real wake-word one — would see
            # it on its first loop check and terminate immediately.
            self._manual_stop.clear()

            try:
                self.on_wake()
            except Exception as e:
                print(f"[wakeword] on_wake callback failed: {e}")

            _drain_pending(self._proc, CHUNK_BYTES)   # flush stale backlog before recording

            audio_chunks = []
            silence_start = None
            # Tracks whether any real speech has been seen yet this
            # utterance. The silence-cutoff clock below must not start
            # until this is True — otherwise a manual trigger_now() (which
            # fires before Dave has said anything, unlike a real
            # wake-word trigger where speech is already underway) sees
            # "silence" from chunk one, the SILENCE_DURATION_S clock
            # expires before he's spoken, and this method returns a
            # near-empty buffer that vad_filter then strips down to
            # nothing in transcribe().
            speech_detected = False
            t0 = time.time()

            while time.time() - t0 < MAX_UTTERANCE_S:
                if self._muted.is_set():
                    # TTS started mid-recording (Clara began speaking a
                    # reply to an earlier turn) — discard rather than
                    # transcribe, since the rest of this recording would
                    # just be her own voice bleeding back through the mic.
                    print("[wakeword] muted mid-recording (TTS started) — discarding utterance")
                    return
                if self._manual_stop.is_set():
                    # Button pressed a second time — Dave is done talking,
                    # end the recording now instead of waiting on the
                    # silence timer or MAX_UTTERANCE_S. Still goes through
                    # the normal on_utterance path below (not a discard),
                    # same as a natural silence-triggered end.
                    self._manual_stop.clear()
                    print("[wakeword] manual stop — ending utterance recording early")
                    break
                raw = self._proc.stdout.read(CHUNK_BYTES)
                if not raw:
                    break
                chunk = _bytes_to_mono_float(raw)
                audio_chunks.append(chunk)

                rms = float(np.sqrt(np.mean(chunk.astype(np.float64) ** 2)))
                if DEBUG:
                    print(f"[wakeword] DEBUG utterance rms={rms:.4f} silence_start={silence_start} speech_detected={speech_detected}")
                if rms < SILENCE_RMS_THRESHOLD:
                    if speech_detected:
                        if silence_start is None:
                            silence_start = time.time()
                        elif time.time() - silence_start >= SILENCE_DURATION_S:
                            break
                    # else: still waiting for speech to start — leading
                    # silence (reaction time after a button press) doesn't
                    # count toward the cutoff.
                else:
                    speech_detected = True
                    silence_start = None

            audio_48k = np.concatenate(audio_chunks) if audio_chunks else np.array([], dtype=np.float32)
            audio_16k = _resample(audio_48k, CAPTURE_RATE, TARGET_RATE)
            try:
                self.on_utterance(audio_16k)
            except Exception as e:
                print(f"[wakeword] on_utterance callback failed: {e}")
        finally:
            self._recording.clear()
