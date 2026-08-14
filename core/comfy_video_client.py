"""
core/comfy_video_client.py — ComfyUI video-generation client.

Built against Dave's exported Wan2.2-14B-T2V API workflow
(video_wan2_2_14B_t2v.json) rather than a generic template, since video
graphs vary far more between models/node-suites than the simple SD1.5
image graph in comfy_client.py — guessing at node IDs would just fail
against the real server. If this workflow is swapped or re-exported with
different node IDs, update the NODE_* constants below to match.
"""

import copy
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import websocket

from config import COMFYUI_HOST, VIDEO_DIR


class ComfyVideoClient:
    """
    Communicate with a local ComfyUI server to queue Wan2.2 14B
    text-to-video prompts and stream progress via WebSocket.
    """

    # Exported via ComfyUI's "Save (API Format)" button. Node IDs below
    # (e.g. "128:89") match this exact graph — they come from a ComfyUI
    # subgraph and are not meant to be renumbered by hand.
    DEFAULT_WORKFLOW = {
        "80": {
            "inputs": {
                "filename_prefix": "video/Clara",
                "format": "auto",
                "codec": "auto",
                "video": ["128:88", 0],
            },
            "class_type": "SaveVideo",
            "_meta": {"title": "Save Video"},
        },
        "128:71": {
            "inputs": {
                "clip_name": "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
                "type": "wan",
                "device": "default",
            },
            "class_type": "CLIPLoader",
            "_meta": {"title": "Load CLIP"},
        },
        "128:73": {
            "inputs": {"vae_name": "wan_2.1_vae.safetensors"},
            "class_type": "VAELoader",
            "_meta": {"title": "Load VAE"},
        },
        "128:76": {
            "inputs": {
                "unet_name": "wan2.2_t2v_low_noise_14B_fp8_scaled.safetensors",
                "weight_dtype": "default",
            },
            "class_type": "UNETLoader",
            "_meta": {"title": "Load Diffusion Model"},
        },
        "128:75": {
            "inputs": {
                "unet_name": "wan2.2_t2v_high_noise_14B_fp8_scaled.safetensors",
                "weight_dtype": "default",
            },
            "class_type": "UNETLoader",
            "_meta": {"title": "Load Diffusion Model"},
        },
        "128:83": {
            "inputs": {
                "lora_name": "wan2.2_t2v_lightx2v_4steps_lora_v1.1_high_noise.safetensors",
                "strength_model": 1.0000000000000002,
                "model": ["128:75", 0],
            },
            "class_type": "LoraLoaderModelOnly",
            "_meta": {"title": "Load LoRA"},
        },
        "128:85": {
            "inputs": {
                "lora_name": "wan2.2_t2v_lightx2v_4steps_lora_v1.1_low_noise.safetensors",
                "strength_model": 1.0000000000000002,
                "model": ["128:76", 0],
            },
            "class_type": "LoraLoaderModelOnly",
            "_meta": {"title": "Load LoRA"},
        },
        "128:86": {
            "inputs": {"shift": 5.000000000000001, "model": ["128:121", 0]},
            "class_type": "ModelSamplingSD3",
            "_meta": {"title": "ModelSamplingSD3"},
        },
        "128:82": {
            "inputs": {"shift": 5.000000000000001, "model": ["128:120", 0]},
            "class_type": "ModelSamplingSD3",
            "_meta": {"title": "ModelSamplingSD3"},
        },
        "128:89": {
            "inputs": {
                "text": "",
                "clip": ["128:71", 0],
            },
            "class_type": "CLIPTextEncode",
            "_meta": {"title": "CLIP Text Encode (Positive Prompt)"},
        },
        "128:81": {
            "inputs": {
                "add_noise": "enable",
                "noise_seed": 923510416338945,
                "steps": ["128:122", 0],
                "cfg": ["128:124", 0],
                "sampler_name": "euler",
                "scheduler": "simple",
                "start_at_step": 0,
                "end_at_step": ["128:123", 0],
                "return_with_leftover_noise": "enable",
                "model": ["128:82", 0],
                "positive": ["128:89", 0],
                "negative": ["128:72", 0],
                "latent_image": ["128:74", 0],
            },
            "class_type": "KSamplerAdvanced",
            "_meta": {"title": "KSampler (Advanced)"},
        },
        "128:88": {
            "inputs": {
                "fps": ["128:125", 0],
                "bit_depth": 8,
                "images": ["128:87", 0],
            },
            "class_type": "CreateVideo",
            "_meta": {"title": "Create Video"},
        },
        "128:87": {
            "inputs": {"samples": ["128:78", 0], "vae": ["128:73", 0]},
            "class_type": "VAEDecode",
            "_meta": {"title": "VAE Decode"},
        },
        "128:72": {
            "inputs": {
                "text": "色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，"
                        "整体发灰，最差质量，低质量，JPEG压缩残留，丑陋的，残缺的，多余的手指，"
                        "画得不好的手部，画得不好的脸部，畸形的，毁容的，形态畸形的肢体，手指融合，"
                        "静止不动的画面，杂乱的背景，三条腿，背景人很多，倒着走，裸露，NSFW",
                "clip": ["128:71", 0],
            },
            "class_type": "CLIPTextEncode",
            "_meta": {"title": "CLIP Text Encode (Negative Prompt)"},
        },
        "128:74": {
            "inputs": {
                "width": 640,
                "height": 640,
                "length": ["128:127", 1],
                "batch_size": 1,
            },
            "class_type": "EmptyHunyuanLatentVideo",
            "_meta": {"title": "Empty HunyuanVideo 1.0 Latent"},
        },
        "128:78": {
            "inputs": {
                "add_noise": "disable",
                "noise_seed": 0,
                "steps": ["128:122", 0],
                "cfg": ["128:124", 0],
                "sampler_name": "euler",
                "scheduler": "simple",
                "start_at_step": ["128:123", 0],
                "end_at_step": ["128:122", 0],
                "return_with_leftover_noise": "disable",
                "model": ["128:86", 0],
                "positive": ["128:89", 0],
                "negative": ["128:72", 0],
                "latent_image": ["128:81", 0],
            },
            "class_type": "KSamplerAdvanced",
            "_meta": {"title": "KSampler (Advanced)"},
        },
        "128:114": {"inputs": {"value": 20}, "class_type": "PrimitiveInt", "_meta": {"title": "Int (Steps)"}},
        "128:115": {"inputs": {"value": 10}, "class_type": "PrimitiveInt", "_meta": {"title": "Int (Split Steps)"}},
        "128:117": {"inputs": {"value": 4}, "class_type": "PrimitiveInt", "_meta": {"title": "Int (Steps)"}},
        "128:118": {"inputs": {"value": 2}, "class_type": "PrimitiveInt", "_meta": {"title": "Int (Split Steps)"}},
        "128:119": {"inputs": {"value": 1}, "class_type": "PrimitiveFloat", "_meta": {"title": "Float(CFG)"}},
        "128:116": {"inputs": {"value": 3.5}, "class_type": "PrimitiveFloat", "_meta": {"title": "Float(CFG)"}},
        "128:120": {
            "inputs": {"switch": ["128:129", 0], "on_false": ["128:75", 0], "on_true": ["128:83", 0]},
            "class_type": "ComfySwitchNode",
            "_meta": {"title": "Switch(high noise model)"},
        },
        "128:121": {
            "inputs": {"switch": ["128:129", 0], "on_false": ["128:76", 0], "on_true": ["128:85", 0]},
            "class_type": "ComfySwitchNode",
            "_meta": {"title": "Switch(low noise model)"},
        },
        "128:122": {
            "inputs": {"switch": ["128:129", 0], "on_false": ["128:114", 0], "on_true": ["128:117", 0]},
            "class_type": "ComfySwitchNode",
            "_meta": {"title": "Switch(steps)"},
        },
        "128:123": {
            "inputs": {"switch": ["128:129", 0], "on_false": ["128:115", 0], "on_true": ["128:118", 0]},
            "class_type": "ComfySwitchNode",
            "_meta": {"title": "Switch(split steps)"},
        },
        "128:124": {
            "inputs": {"switch": ["128:129", 0], "on_false": ["128:116", 0], "on_true": ["128:119", 0]},
            "class_type": "ComfySwitchNode",
            "_meta": {"title": "Switch(CFG)"},
        },
        "128:125": {"inputs": {"value": 16}, "class_type": "PrimitiveFloat", "_meta": {"title": "Float (FPS)"}},
        "128:126": {"inputs": {"value": 5}, "class_type": "PrimitiveFloat", "_meta": {"title": "Float (Duration)"}},
        "128:129": {"inputs": {"value": False}, "class_type": "PrimitiveBoolean", "_meta": {"title": "Enable Lightning LoRA"}},
        "128:127": {
            "inputs": {
                "expression": "floor(a * b) + 1",
                "values.a": ["128:126", 0],
                "values.b": ["128:125", 0],
            },
            "class_type": "ComfyMathExpression",
            "_meta": {"title": "Math Expression"},
        },
    }

    # Node IDs patched per-generation. Named here so a future workflow
    # swap only requires updating this block, not hunting through generate().
    NODE_POSITIVE          = "128:89"
    NODE_NEGATIVE          = "128:72"
    NODE_LATENT            = "128:74"   # width / height
    NODE_FPS               = "128:125"
    NODE_DURATION          = "128:126"  # seconds
    NODE_SEED              = "128:81"   # noise_seed on the high-noise KSamplerAdvanced
    NODE_LIGHTNING_TOGGLE  = "128:129"
    NODE_STEPS_NORMAL      = "128:114"
    NODE_CFG_NORMAL        = "128:116"
    NODE_STEPS_LIGHTNING   = "128:117"
    NODE_CFG_LIGHTNING     = "128:119"
    NODE_SAVE              = "80"

    def __init__(self, host: str = COMFYUI_HOST):
        self.host = host
        self.client_id = str(uuid.uuid4())
        self._stop_flag = threading.Event()
        self.last_video_path: str | None = None

    # ── Connectivity ───────────────────────────────────────────

    def is_running(self) -> bool:
        try:
            urllib.request.urlopen(f"http://{self.host}/system_stats", timeout=2)
            return True
        except Exception:
            return False

    # ── Prompt API ────────────────────────────────────────────

    def queue_prompt(self, workflow: dict) -> dict:
        payload = json.dumps({"prompt": workflow, "client_id": self.client_id}).encode("utf-8")
        req = urllib.request.Request(
            f"http://{self.host}/prompt",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"ComfyUI rejected prompt: {e.read().decode()}") from e

    def get_video_bytes(self, filename: str, subfolder: str, folder_type: str) -> bytes:
        params = urllib.parse.urlencode(
            {"filename": filename, "subfolder": subfolder, "type": folder_type}
        )
        with urllib.request.urlopen(f"http://{self.host}/view?{params}") as r:
            return r.read()

    def get_history(self, prompt_id: str) -> dict:
        with urllib.request.urlopen(f"http://{self.host}/history/{prompt_id}") as r:
            return json.loads(r.read())

    # ── Generation ────────────────────────────────────────────

    def generate(
        self,
        positive_prompt: str,
        negative_prompt: str = "",
        width: int = 640,
        height: int = 640,
        duration_seconds: float = 5,
        fps: float = 16,
        seed: int = -1,
        use_lightning_lora: bool = False,
        steps: int | None = None,
        cfg: float | None = None,
        progress_callback=None,
        done_callback=None,
        error_callback=None,
    ) -> None:
        """
        Queue and stream a video-generation job.
        All callbacks are invoked from a background thread.

        use_lightning_lora toggles node 128:129, which flips the graph's
        switch nodes between the normal 20-step / cfg-3.5 path and the
        4-step LightX2V LoRA fast path (with its own steps/cfg primitives).
        steps/cfg, if given, override whichever path is currently active —
        they do NOT apply to both paths at once.
        """
        self._stop_flag.clear()
        wf = copy.deepcopy(self.DEFAULT_WORKFLOW)

        wf[self.NODE_POSITIVE]["inputs"]["text"] = positive_prompt
        if negative_prompt:
            wf[self.NODE_NEGATIVE]["inputs"]["text"] = negative_prompt
        wf[self.NODE_LATENT]["inputs"]["width"] = width
        wf[self.NODE_LATENT]["inputs"]["height"] = height
        wf[self.NODE_FPS]["inputs"]["value"] = fps
        wf[self.NODE_DURATION]["inputs"]["value"] = duration_seconds
        wf[self.NODE_SEED]["inputs"]["noise_seed"] = int(time.time()) if seed < 0 else seed
        wf[self.NODE_LIGHTNING_TOGGLE]["inputs"]["value"] = bool(use_lightning_lora)

        if steps is not None:
            key = self.NODE_STEPS_LIGHTNING if use_lightning_lora else self.NODE_STEPS_NORMAL
            wf[key]["inputs"]["value"] = steps
        if cfg is not None:
            key = self.NODE_CFG_LIGHTNING if use_lightning_lora else self.NODE_CFG_NORMAL
            wf[key]["inputs"]["value"] = cfg

        threading.Thread(
            target=self._worker,
            args=(wf, progress_callback, done_callback, error_callback),
            daemon=True,
        ).start()

    def _worker(self, wf, progress_callback, done_callback, error_callback):
        try:
            if progress_callback:
                progress_callback("Sending prompt to ComfyUI...", 0)

            result = self.queue_prompt(wf)
            prompt_id = result["prompt_id"]

            if progress_callback:
                progress_callback("Prompt queued, waiting for GPU...", 5)

            ws_url = f"ws://{self.host}/ws?clientId={self.client_id}"
            ws = websocket.WebSocket()
            ws.connect(ws_url)

            try:
                while not self._stop_flag.is_set():
                    raw = ws.recv()
                    if isinstance(raw, bytes):
                        continue  # binary preview frame — skip

                    msg = json.loads(raw)
                    mtype = msg.get("type", "")

                    if mtype == "progress":
                        d = msg["data"]
                        val = d.get("value", 0)
                        mx = d.get("max", 1)
                        pct = int(val / mx * 100) if mx else 0
                        # This graph runs two KSamplerAdvanced stages back
                        # to back (high-noise pass, then low-noise refine),
                        # so this progress value resets partway through —
                        # it's per-stage, not whole-job. Capped below 100
                        # so it doesn't falsely read "done" mid-job.
                        if progress_callback:
                            progress_callback(f"Generating... step {val}/{mx}", min(pct, 95))

                    elif mtype == "executing":
                        if msg["data"].get("node") is None:
                            break  # generation complete

                    elif mtype == "execution_error":
                        err = msg["data"].get("exception_message", "Unknown error")
                        if error_callback:
                            error_callback(f"ComfyUI error: {err}")
                        return
            finally:
                ws.close()

            if self._stop_flag.is_set():
                if error_callback:
                    error_callback("Generation cancelled.")
                return

            if progress_callback:
                progress_callback("Retrieving video...", 98)

            history = self.get_history(prompt_id)
            outputs = history[prompt_id]["outputs"]
            save_output = outputs.get(self.NODE_SAVE, {})

            # NOTE: unconfirmed against a live server. ComfyUI's base
            # SaveVideo node most likely reuses the "images" key (the
            # frontend's gallery component displays video thumbnails the
            # same way it does images), but some video node-suites
            # (VideoHelperSuite etc.) use "gifs" instead. Checking both;
            # if neither hits, the error message below reports the actual
            # keys so this is easy to fix in one line once you see it.
            video_info_list = save_output.get("images") or save_output.get("gifs") or []

            for info in video_info_list:
                video_data = self.get_video_bytes(
                    info["filename"], info.get("subfolder", ""), info.get("type", "output")
                )
                ts = time.strftime("%Y%m%d_%H%M%S")
                ext = info["filename"].rsplit(".", 1)[-1] if "." in info["filename"] else "mp4"
                save_path = VIDEO_DIR / f"clara_{ts}.{ext}"
                with open(save_path, "wb") as f:
                    f.write(video_data)
                self.last_video_path = str(save_path)

                if done_callback:
                    done_callback(str(save_path))
                return

            if error_callback:
                error_callback(
                    f"No video found in ComfyUI output for node {self.NODE_SAVE} "
                    f"(got keys: {list(save_output.keys())}). Check the ComfyUI "
                    f"history/console to see the actual output key and update "
                    f"get_video_bytes() lookup in comfy_video_client.py accordingly."
                )

        except Exception as ex:
            if error_callback:
                error_callback(str(ex))

    # ── Cancel ────────────────────────────────────────────────

    def cancel(self) -> None:
        self._stop_flag.set()
        try:
            urllib.request.urlopen(f"http://{self.host}/interrupt", data=b"", timeout=3)
        except Exception:
            pass
