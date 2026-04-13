#!/usr/bin/env python3
import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.expanduser("~/ComfyUI/user/tag-selector")
COMFY = "http://127.0.0.1:8189"
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8799"))
DEFAULT_GENERATE_TIMEOUT = 900
MAX_GENERATE_TIMEOUT = 1800
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "bubble-hentai-illustrious-v10-sdxl")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def _fetch_loader_options(self, loader_name, input_name):
        try:
            with urllib.request.urlopen(f"{COMFY}/object_info/{loader_name}", timeout=15) as r:
                info = json.loads(r.read().decode("utf-8"))
            models = (
                info.get(loader_name, {})
                .get("input", {})
                .get("required", {})
                .get(input_name, [[]])[0]
            )
            if isinstance(models, list):
                return [m for m in models if isinstance(m, str)]
        except Exception:
            pass
        return []

    def _fetch_model_catalog(self):
        checkpoints = self._fetch_loader_options("CheckpointLoaderSimple", "ckpt_name")
        diffusers = self._fetch_loader_options("DiffusersLoader", "model_path")
        merged = []
        seen = set()
        for name in checkpoints + diffusers:
            if name not in seen:
                merged.append(name)
                seen.add(name)
        return {
            "checkpoints": checkpoints,
            "diffusers": diffusers,
            "all": merged,
        }

    def _json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/models":
            catalog = self._fetch_model_catalog()
            models = catalog["all"]
            default_model = DEFAULT_MODEL if DEFAULT_MODEL in models else (models[0] if models else DEFAULT_MODEL)
            self._json(
                200,
                {
                    "models": models,
                    "default": default_model,
                    "checkpoints": catalog["checkpoints"],
                    "diffusers": catalog["diffusers"],
                },
            )
            return
        super().do_GET()

    def _find_asset_id_by_name(self, filename):
        """Return the asset UUID for an exact filename match, or None."""
        encoded = urllib.parse.urlencode({"name": filename, "limit": 50})
        with urllib.request.urlopen(f"{COMFY}/api/assets?{encoded}", timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
        for asset in data.get("assets", []):
            if asset.get("name") == filename:
                return asset["id"]
        return None

    def do_DELETE(self):
        # DELETE /api/assets/<filename>?delete_content=true
        if not self.path.startswith("/api/assets/"):
            self._json(404, {"error": "not found"})
            return

        filename = urllib.parse.unquote(self.path[len("/api/assets/"):].split("?")[0])
        delete_content = "delete_content=true" in self.path

        try:
            asset_id = self._find_asset_id_by_name(filename)
        except Exception as e:
            self._json(502, {"error": f"Failed to query assets: {e}"})
            return

        if not asset_id:
            self._json(404, {"error": f"No asset found with name '{filename}'"})
            return

        qs = "?delete_content=true" if delete_content else ""
        req = urllib.request.Request(
            f"{COMFY}/api/assets/{asset_id}{qs}",
            method="DELETE",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                status = r.status
        except urllib.error.HTTPError as e:
            status = e.code

        if status == 204:
            self._json(200, {"deleted": True, "id": asset_id, "name": filename})
        else:
            self._json(status, {"error": f"ComfyUI returned {status}", "id": asset_id})

    def do_POST(self):
        if self.path != "/api/generate":
            self._json(404, {"error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            req = json.loads(raw.decode("utf-8"))

            positive = req.get("prompt", req.get("positive", ""))
            negative = req.get("negative", "")
            width = int(req.get("width", 832))
            height = int(req.get("height", 1216))
            steps = int(req.get("steps", 28))
            cfg = float(req.get("cfg", 5))
            seed = int(req.get("seed", int(time.time()) % 1000000000))
            timeout_s = int(req.get("timeout_seconds", DEFAULT_GENERATE_TIMEOUT))
            model = str(req.get("model", DEFAULT_MODEL)).strip() or DEFAULT_MODEL
            catalog = self._fetch_model_catalog()
            available_models = catalog["all"]

            if not positive.strip():
                self._json(400, {"error": "positive prompt is required"})
                return

            if available_models and model not in available_models:
                self._json(
                    400,
                    {
                        "error": f"Model '{model}' is not installed",
                        "available_models": available_models,
                    },
                )
                return

            if model in catalog["checkpoints"]:
                loader_class = "CheckpointLoaderSimple"
                loader_inputs = {"ckpt_name": model}
            elif model in catalog["diffusers"]:
                loader_class = "DiffusersLoader"
                loader_inputs = {"model_path": model}
            else:
                loader_class = "CheckpointLoaderSimple"
                loader_inputs = {"ckpt_name": model}

            timeout_s = max(60, min(timeout_s, MAX_GENERATE_TIMEOUT))

            payload = {
                "prompt": {
                    "1": {
                        "class_type": loader_class,
                        "inputs": loader_inputs,
                    },
                    "2": {
                        "class_type": "CLIPTextEncode",
                        "inputs": {"text": positive, "clip": ["1", 1]},
                    },
                    "3": {
                        "class_type": "CLIPTextEncode",
                        "inputs": {"text": negative, "clip": ["1", 1]},
                    },
                    "4": {
                        "class_type": "EmptyLatentImage",
                        "inputs": {"width": width, "height": height, "batch_size": 1},
                    },
                    "5": {
                        "class_type": "KSampler",
                        "inputs": {
                            "seed": seed,
                            "steps": steps,
                            "cfg": cfg,
                            "sampler_name": "euler",
                            "scheduler": "normal",
                            "denoise": 1,
                            "model": ["1", 0],
                            "positive": ["2", 0],
                            "negative": ["3", 0],
                            "latent_image": ["4", 0],
                        },
                    },
                    "6": {
                        "class_type": "VAEDecode",
                        "inputs": {"samples": ["5", 0], "vae": ["1", 2]},
                    },
                    "7": {
                        "class_type": "SaveImage",
                        "inputs": {"filename_prefix": "animagine_api", "images": ["6", 0]},
                    },
                }
            }

            prompt_req = urllib.request.Request(
                f"{COMFY}/prompt",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(prompt_req, timeout=30) as r:
                    prompt_resp = json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                details = ""
                try:
                    details = e.read().decode("utf-8", errors="replace")
                except Exception:
                    details = ""
                msg = f"ComfyUI /prompt failed ({e.code})"
                if details:
                    msg += f": {details}"
                self._json(500, {"error": msg})
                return

            prompt_id = prompt_resp.get("prompt_id")
            if not prompt_id:
                self._json(500, {"error": "Missing prompt_id from ComfyUI"})
                return

            image_meta = None
            started = time.time()
            deadline = started + timeout_s
            while time.time() < deadline:
                with urllib.request.urlopen(f"{COMFY}/history/{prompt_id}", timeout=20) as r:
                    hist = json.loads(r.read().decode("utf-8"))

                run = hist.get(prompt_id)
                if run and run.get("outputs"):
                    for out in run.get("outputs", {}).values():
                        images = out.get("images", [])
                        if images:
                            image_meta = images[0]
                            break
                if image_meta:
                    break
                time.sleep(1)

            if not image_meta:
                elapsed = int(time.time() - started)
                self._json(
                    504,
                    {
                        "error": "Timed out waiting for image generation",
                        "prompt_id": prompt_id,
                        "elapsed_seconds": elapsed,
                        "timeout_seconds": timeout_s,
                    },
                )
                return

            params = urllib.parse.urlencode(
                {
                    "filename": image_meta.get("filename", ""),
                    "subfolder": image_meta.get("subfolder", ""),
                    "type": image_meta.get("type", "output"),
                }
            )
            with urllib.request.urlopen(f"{COMFY}/view?{params}", timeout=60) as r:
                img_bytes = r.read()

            data_url = "data:image/png;base64," + base64.b64encode(img_bytes).decode("ascii")
            self._json(
                200,
                {
                    "prompt_id": prompt_id,
                    "filename": image_meta.get("filename", ""),
                    "model": model,
                    "loader": loader_class,
                    "data_url": data_url,
                },
            )
        except Exception as e:
            self._json(500, {"error": str(e)})


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    shown_host = HOST if HOST != "0.0.0.0" else "<all-interfaces>"
    print(f"Animagine tag server at http://{shown_host}:{PORT}", flush=True)
    server.serve_forever()
