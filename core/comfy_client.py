"""
core/comfy_client.py — Thin wrapper around the ComfyUI HTTP + WebSocket API.
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

from config import COMFYUI_HOST, IMAGE_DIR


class ComfyUIClient:
    """
    Communicate with a local ComfyUI server to queue image-generation
    prompts and stream progress via WebSocket.
    """

    # Default Stable-Diffusion text-to-image workflow
    DEFAULT_WORKFLOW = {
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "cfg": 7,
                "denoise": 1,
                "latent_image": ["5", 0],
                "model": ["4", 0],
                "negative": ["7", 0],
                "positive": ["6", 0],
                "sampler_name": "euler",
                "scheduler": "normal",
                "seed": 0,
                "steps": 20,
            },
        },
        "4": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": "v1-5-pruned-emaonly.ckpt"},
        },
        "5": {
            "class_type": "EmptyLatentImage",
            "inputs": {"batch_size": 1, "height": 1024, "width": 1024},
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {"clip": ["4", 1], "text": "POSITIVE_PROMPT"},
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {"clip": ["4", 1], "text": "NEGATIVE_PROMPT"},
        },
        "8": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["3", 0], "vae": ["4", 2]},
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": "Clara", "images": ["8", 0]},
        },
    }

    def __init__(self, host: str = COMFYUI_HOST):
        self.host = host
        self.client_id = str(uuid.uuid4())
        self._stop_flag = threading.Event()
        self.last_image_path: str | None = None

    # ── Connectivity ───────────────────────────────────────────

    def is_running(self) -> bool:
        try:
            urllib.request.urlopen(
                f"http://{self.host}/system_stats", timeout=2
            )
            return True
        except Exception:
            return False

    # ── Model / sampler discovery ──────────────────────────────

    def get_checkpoints(self) -> list[str]:
        try:
            url = f"http://{self.host}/object_info/CheckpointLoaderSimple"
            with urllib.request.urlopen(url, timeout=5) as r:
                data = json.loads(r.read())
            return data["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
        except Exception:
            return []

    def get_samplers(self) -> tuple[list[str], list[str]]:
        try:
            url = f"http://{self.host}/object_info/KSampler"
            with urllib.request.urlopen(url, timeout=5) as r:
                data = json.loads(r.read())
            samplers   = data["KSampler"]["input"]["required"]["sampler_name"][0]
            schedulers = data["KSampler"]["input"]["required"]["scheduler"][0]
            return samplers, schedulers
        except Exception:
            return [], []

    # ── Prompt API ────────────────────────────────────────────

    def queue_prompt(self, workflow: dict) -> dict:
        payload = json.dumps(
            {"prompt": workflow, "client_id": self.client_id}
        ).encode("utf-8")
        req = urllib.request.Request(
            f"http://{self.host}/prompt",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(
                f"ComfyUI rejected prompt: {e.read().decode()}"
            ) from e

    def get_image(self, filename: str, subfolder: str, folder_type: str) -> bytes:
        params = urllib.parse.urlencode(
            {"filename": filename, "subfolder": subfolder, "type": folder_type}
        )
        with urllib.request.urlopen(f"http://{self.host}/view?{params}") as r:
            return r.read()

    def get_history(self, prompt_id: str) -> dict:
        with urllib.request.urlopen(
            f"http://{self.host}/history/{prompt_id}"
        ) as r:
            return json.loads(r.read())

    # ── Generation ────────────────────────────────────────────

    def generate(
        self,
        positive_prompt: str,
        negative_prompt: str = "",
        checkpoint: str | None = None,
        width: int = 1024,
        height: int = 1024,
        steps: int = 20,
        cfg: float = 7.0,
        sampler: str = "euler",
        scheduler: str = "normal",
        seed: int = -1,
        progress_callback=None,
        done_callback=None,
        error_callback=None,
    ) -> None:
        """
        Queue and stream an image-generation job.
        All callbacks are invoked from a background thread.
        """
        self._stop_flag.clear()
        wf = copy.deepcopy(self.DEFAULT_WORKFLOW)

        # Patch workflow
        if checkpoint:
            wf["4"]["inputs"]["ckpt_name"] = checkpoint
        wf["5"]["inputs"]["width"]           = width
        wf["5"]["inputs"]["height"]          = height
        wf["3"]["inputs"]["steps"]           = steps
        wf["3"]["inputs"]["cfg"]             = cfg
        wf["3"]["inputs"]["sampler_name"]    = sampler
        wf["3"]["inputs"]["scheduler"]       = scheduler
        wf["3"]["inputs"]["seed"]            = int(time.time()) if seed < 0 else seed
        wf["6"]["inputs"]["text"]            = positive_prompt
        wf["7"]["inputs"]["text"]            = (
            negative_prompt or "blurry, bad quality, watermark, text, signature"
        )

        threading.Thread(
            target=self._worker,
            args=(wf, progress_callback, done_callback, error_callback),
            daemon=True,
        ).start()

    def _worker(self, wf, progress_callback, done_callback, error_callback):
        try:
            if progress_callback:
                progress_callback("Sending prompt to ComfyUI...", 0)

            result    = self.queue_prompt(wf)
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

                    msg   = json.loads(raw)
                    mtype = msg.get("type", "")

                    if mtype == "progress":
                        d   = msg["data"]
                        val = d.get("value", 0)
                        mx  = d.get("max", 1)
                        pct = int(val / mx * 100) if mx else 0
                        if progress_callback:
                            progress_callback(f"Generating... step {val}/{mx}", pct)

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

            # Retrieve saved image
            if progress_callback:
                progress_callback("Retrieving image...", 99)

            history = self.get_history(prompt_id)
            outputs = history[prompt_id]["outputs"]

            for _node_id, node_output in outputs.items():
                if "images" in node_output:
                    for img_info in node_output["images"]:
                        img_data = self.get_image(
                            img_info["filename"],
                            img_info["subfolder"],
                            img_info["type"],
                        )
                        ts        = time.strftime("%Y%m%d_%H%M%S")
                        save_path = IMAGE_DIR / f"clara_{ts}.png"
                        with open(save_path, "wb") as f:
                            f.write(img_data)
                        self.last_image_path = str(save_path)

                        w = img_info.get("width",  wf["5"]["inputs"]["width"])
                        h = img_info.get("height", wf["5"]["inputs"]["height"])

                        if done_callback:
                            done_callback(img_data, w, h, str(save_path))
                        return

            if error_callback:
                error_callback("No images found in ComfyUI output.")

        except Exception as ex:
            if error_callback:
                error_callback(str(ex))

    # ── Cancel ────────────────────────────────────────────────

    def cancel(self) -> None:
        self._stop_flag.set()
        try:
            urllib.request.urlopen(
                f"http://{self.host}/interrupt", data=b"", timeout=3
            )
        except Exception:
            pass
