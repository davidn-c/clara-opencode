import os
import io
from fastapi.responses import Response
import re
import warnings
import numpy as np
import torch
from scipy.io import wavfile
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import PlainTextResponse
import uvicorn
from faster_qwen3_tts import FasterQwen3TTS
import time
import logging
logging.basicConfig(level=logging.INFO, format="%(message)s")

# 1. Silence warnings and optimize memory allocations
warnings.filterwarnings("ignore", category=UserWarning)
os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"

PORT = 5050
FIXED_SEED = 42

REF_AUDIO_PATH = "/home/resources/reference.wav"
REF_TEXT = "It was in the first place. After the strangest fashion. A sense of the extraordinary way in which the most benign conditions of light, and air, of sky and sea. The most beautiful English summer conceivable."

DEFAULT_INSTRUCT = (
    "Inflection: Do not try to inflect on words.  Stay constant. Gender: Female. Pace: normal. Pitch: warm, mid-range."
    "Emotion: calm, friendly, conversational. Delivery: even and consistent across all sentences, "
    "not exaggerated. Stay in character, do not jump around in how you deliver your response."
    "Carry over the same exact style as every sentence before the one you are speaking."
)

app = FastAPI()


def fix_pronunciations(text):
    text = re.sub(r'(\d+)\s*°F', r'\1 degrees Fahrenheit', text)
    text = re.sub(r'(\d+)\s*°C', r'\1 degrees Celsius', text)
    text = re.sub(r'(\d+)\s*degrees\s*F\b', r'\1 degrees Fahrenheit', text)
    text = re.sub(r'(\d+)\s*degrees\s*C\b', r'\1 degrees Celsius', text)
    return text

print("Loading Qwen3-TTS-12Hz-1.7B-Base via CUDA Graph capture...")
pipeline = FasterQwen3TTS.from_pretrained(
    "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
    device="cuda",
    max_seq_len=768
)

print(f"Creating voice clone prompt from {REF_AUDIO_PATH}...")
VOICE_CLONE_PROMPT = pipeline.model.create_voice_clone_prompt(
    ref_audio=REF_AUDIO_PATH,
    ref_text=REF_TEXT,
    x_vector_only_mode=False,
)
print(f"Qwen3-TTS (Base/clone) ready. Listening on port {PORT}")

print("Running warmup generation...")
_ = pipeline.generate_voice_clone(
    text="Warming up.",
    language="English",
    voice_clone_prompt=VOICE_CLONE_PROMPT,
    temperature=0.7,
    top_p=0.9,
    top_k=20,
    repetition_penalty=1.05,
)
torch.cuda.empty_cache()
print("Warmup complete.")

async def process_tts(
    text: str,
    background_tasks: BackgroundTasks,
    instruct: str = None,
    temperature: float = 0.7,
    top_p: float = 0.9,
    top_k: int = 20,
    repetition_penalty: float = 1.05,
):
    # 1. Type Guard: Force cast to string and block empty or malformed structures
    if text is None or not isinstance(text, str) or not text.strip():
        print("[Warning] Received empty or invalid text payload. Skipping.")
        text = "Next sentence."

    text = fix_pronunciations(text)
    print(f"Generating audio for: {text[:50]}...")

    torch.manual_seed(FIXED_SEED)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(FIXED_SEED)

    t0 = time.time()
    print(f"[tts] requesting generation for {len(text)} chars")
    audio, sr = pipeline.generate_voice_clone(
        text=text,
        language="English",
        voice_clone_prompt=VOICE_CLONE_PROMPT,
        temperature=temperature,
        top_p=top_p,
        top_k=top_k,
        repetition_penalty=repetition_penalty,
    )
    torch.cuda.empty_cache()
    print(f"[TIMING] generation took {time.time() - t0:.2f}s")

    audio_merged = np.concatenate(audio, axis=-1)
    if audio_merged.ndim > 1:
        audio_merged = audio_merged.flatten()

    # Convert float32 [-1, 1] to int16 for pw-play --format=s16
    audio_int16 = (audio_merged * 32767).astype(np.int16)

    buf = io.BytesIO()
    buf.write(audio_int16.tobytes())
    buf.seek(0)

    # Free fragmented allocations from this generation call before the next
    # one lands. Without this, successive chunks within one long reply
    # (e.g. a search-summary answer that fires many TTS calls back-to-back)
    # accumulate until the 2060's 5.6GB ceiling is hit mid-session, even
    # though no single chunk is large. Runs after the response is already
    # sent, so it doesn't add latency to this request.
    background_tasks.add_task(torch.cuda.empty_cache)

    return Response(
        content=buf.read(),
        media_type="audio/pcm",
        headers={"X-Sample-Rate": str(sr)}
    )

@app.get("/ping")
async def ping():
    return PlainTextResponse(content="OK")


@app.get("/tts/{text}")
async def generate_tts_get(text: str, background_tasks: BackgroundTasks):
    return await process_tts(text, background_tasks)


@app.post("/tts")
async def generate_tts_post(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()
    text = data.get("text", "")
    return await process_tts(
        text,
        background_tasks,
        instruct=data.get("instruct"),
        temperature=data.get("temperature", 0.7),
        top_p=data.get("top_p", 0.9),
        top_k=data.get("top_k", 20),
        repetition_penalty=data.get("repetition_penalty", 1.05),
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
