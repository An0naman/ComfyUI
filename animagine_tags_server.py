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

    def _refresh_assets(self, roots, wait):
        payload = {"roots": roots}
        qs = "?wait=true" if wait else ""
        req = urllib.request.Request(
            f"{COMFY}/api/assets/seed{qs}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode("utf-8"))

    def _find_history_prompt_ids_by_filename(self, filename, max_items=2000):
        encoded = urllib.parse.urlencode({"max_items": max_items})
        with urllib.request.urlopen(f"{COMFY}/history?{encoded}", timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))

        prompt_ids = []
        if not isinstance(data, dict):
            return prompt_ids

        for prompt_id, item in data.items():
            outputs = (item or {}).get("outputs", {})
            found = False
            for node_output in outputs.values():
                for image in node_output.get("images", []):
                    if image.get("filename") == filename:
                        found = True
                        break
                if found:
                    break
            if found:
                prompt_ids.append(prompt_id)
        return prompt_ids

    def _delete_history_prompt_ids(self, prompt_ids):
        if not prompt_ids:
            return 0
        payload = {"delete": prompt_ids}
        req = urllib.request.Request(
            f"{COMFY}/history",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20):
            pass
        return len(prompt_ids)

    def do_DELETE(self):
        # DELETE /api/assets/<filename>?delete_content=true
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path

        if not route.startswith("/api/assets/"):
            self._json(404, {"error": "not found"})
            return

        filename = urllib.parse.unquote(route[len("/api/assets/"):])
        query = urllib.parse.parse_qs(parsed.query)
        delete_content = str(query.get("delete_content", ["false"])[0]).lower() in {
            "1",
            "true",
            "yes",
        }
        purge_history = str(query.get("purge_history", ["true"])[0]).lower() in {
            "1",
            "true",
            "yes",
        }
        refresh_assets = str(query.get("refresh_assets", ["true"])[0]).lower() in {
            "1",
            "true",
            "yes",
        }

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
            history_deleted = 0
            history_error = None
            if purge_history:
                try:
                    prompt_ids = self._find_history_prompt_ids_by_filename(filename)
                    history_deleted = self._delete_history_prompt_ids(prompt_ids)
                except Exception as e:
                    history_error = str(e)

            refresh_error = None
            refresh_result = None
            if refresh_assets:
                try:
                    _, refresh_result = self._refresh_assets(["output"], True)
                except Exception as e:
                    refresh_error = str(e)

            response = {
                "deleted": True,
                "id": asset_id,
                "name": filename,
                "history_deleted": history_deleted,
                "assets_refreshed": bool(refresh_assets and refresh_error is None),
            }
            if refresh_result is not None:
                response["refresh_result"] = refresh_result
            if history_error:
                response["history_error"] = history_error
            if refresh_error:
                response["refresh_error"] = refresh_error
            self._json(200, response)
        else:
            self._json(status, {"error": f"ComfyUI returned {status}", "id": asset_id})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path

        if route == "/api/assets/refresh":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length > 0 else b""
                req = json.loads(raw.decode("utf-8")) if raw else {}

                roots = req.get("roots", ["output"])
                if not isinstance(roots, list):
                    self._json(400, {"error": "roots must be a list"})
                    return
                roots = [r for r in roots if r in {"models", "input", "output"}]
                if not roots:
                    roots = ["output"]

                wait = req.get("wait", True)
                wait = bool(wait)

                status, result = self._refresh_assets(roots, wait)
                self._json(
                    status,
                    {
                        "ok": status in (200, 202),
                        "comfy_status": status,
                        "roots": roots,
                        "result": result,
                    },
                )
                return
            except urllib.error.HTTPError as e:
                details = ""
                try:
                    details = e.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                self._json(502, {"error": f"ComfyUI refresh failed ({e.code})", "details": details})
                return
            except Exception as e:
                self._json(500, {"error": str(e)})
                return

        if route != "/api/generate":
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
