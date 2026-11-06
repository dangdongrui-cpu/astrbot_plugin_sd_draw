"""后端双引擎适配：WebUI（默认）优先，ComfyUI 兜底。

WebUI 走自带 /sdapi/v1/txt2img（所有 A1111/Forge 都有，稳）；
ComfyUI 走 /prompt 提交 workflow + /history 轮询结果。
"""
from __future__ import annotations

import base64
import json
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from ..config import SdDrawConfig

DEFAULT_COMFY_WORKFLOW = {
    "3": {
        "class_type": "KSampler",
        "inputs": {
            "seed": 42,
            "steps": 28,
            "cfg": 7.0,
            "sampler_name": "dpmpp_2m",
            "scheduler": "karras",
            "denoise": 1.0,
            "model": ["4", 0],
            "positive": ["6", 0],
            "negative": ["7", 0],
            "latent_image": ["5", 0],
        },
    },
    "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ""}},
    "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512, "batch_size": 1}},
    "6": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "", "clip": ["4", 1]},
    },
    "7": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "", "clip": ["4", 1]},
    },
    "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
    "9": {
        "class_type": "SaveImage",
        "inputs": {"filename_prefix": "sd_draw", "images": ["8", 0]},
    },
}


@dataclass
class GenerationRequest:
    prompt: str
    negative_prompt: str = ""
    width: int = 512
    height: int = 512
    steps: int = 28
    cfg_scale: float = 7.0
    sampler: str = "DPM++ 2M Karras"
    seed: int = -1
    hires: bool = False
    """是否启用 hires fix 高清修复"""
    hires_upscaler: str = "R-ESRGAN 4x+ Anime6B"
    """高清修复放大器名"""
    hires_scale: float = 1.4
    """高清放大倍率"""
    hires_denoise: float = 0.4
    """二遍重绘强度"""
    model: str = ""
    """checkpoint 名（空则用后端默认）"""
    workflow: dict[str, Any] = field(default_factory=dict)
    """ComfyUI 自定义 workflow（空则用默认文生图流程）"""


@dataclass
class GenerationResult:
    image_bytes: bytes
    info: dict[str, Any] = field(default_factory=dict)
    backend: str = ""


class BackendResultError(Exception):
    pass


class SDWebUIAdapter:
    name = "webui"

    def __init__(self, cfg: SdDrawConfig):
        self.cfg = cfg
        self._norm_model_cache: dict[str, str] = {}

    def _url(self, path: str) -> str:
        return self.cfg.webui_url.rstrip("/") + path

    async def health(self) -> bool:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(self._url("/sdapi/v1/options"), timeout=aiohttp.ClientTimeout(total=5)) as r:
                    return r.status == 200
        except Exception:
            return False

    async def _resolve_model(self, aio_session: aiohttp.ClientSession) -> str:
        """把模糊的模型名匹配到后端 sd_model_checkpoint 的真实名。"""
        name = (self.cfg.default_model or "").strip()
        if not name:
            return ""
        if name in self._norm_model_cache:
            return self._norm_model_cache[name]
        resolved = ""
        try:
            async with aio_session.get(self._url("/sdapi/v1/sd-models"), timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    models = await r.json()
                    for m in models:
                        title = (m.get("title") or "").lower()
                        mdl_name = (m.get("model_name") or "").lower()
                        key = name.lower()
                        if key in title or key in mdl_name or key + ".safetensors" in title:
                            resolved = m.get("title") or m.get("model_name") or ""
                            break
        except Exception:
            resolved = ""
        self._norm_model_cache[name] = resolved
        return resolved

    async def generate(self, req: GenerationRequest) -> GenerationResult:
        payload = {
            "prompt": req.prompt,
            "negative_prompt": req.negative_prompt,
            "width": int(req.width),
            "height": int(req.height),
            "steps": int(req.steps),
            "cfg_scale": float(req.cfg_scale),
            "sampler_name": req.sampler,
            "seed": int(req.seed) if req.seed and req.seed >= 0 else -1,
        }
        if req.hires:
            payload["enable_hr"] = True
            payload["hr_upscaler"] = req.hires_upscaler or "R-ESRGAN 4x+ Anime6B"
            payload["hr_scale"] = float(req.hires_scale)
            payload["denoising_strength"] = float(req.hires_denoise)
            payload["hr_second_pass_steps"] = int(req.steps)
        async with aiohttp.ClientSession() as s:
            mdl = await self._resolve_model(s)
            if mdl:
                payload["override_settings"] = {"sd_model_checkpoint": mdl}
            async with s.post(
                self._url("/sdapi/v1/txt2img"),
                json=payload,
                timeout=aiohttp.ClientTimeout(total=self.cfg.backend_timeout),
            ) as r:
                if r.status != 200:
                    detail = (await r.text())[:500]
                    raise BackendResultError(f"WebUI txt2img 返回 {r.status}: {detail}")
                data = await r.json()
        images = data.get("images") or []
        if not images:
            raise BackendResultError("WebUI 未返回图片数据")
        raw = images[0]
        if raw.startswith("data:"):
            raw = raw.split(",", 1)[1]
        try:
            img_bytes = base64.b64decode(raw)
        except Exception as e:
            raise BackendResultError(f"图片解码失败: {e}") from e
        return GenerationResult(image_bytes=img_bytes, info={}, backend=self.name)

    def list_models_script(self) -> str:
        return ""  # 模型清单走 webui /sdapi/v1/sd-models


class ComfyUIAdapter:
    name = "comfyui"

    def __init__(self, cfg: SdDrawConfig):
        self.cfg = cfg

    def _url(self, path: str) -> str:
        return self.cfg.comfyui_url.rstrip("/") + path

    async def health(self) -> bool:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(self._url("/system_stats"), timeout=aiohttp.ClientTimeout(total=5)) as r:
                    return r.status == 200
        except Exception:
            return False

    async def list_checkpoints(self) -> list[str]:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(self._url("/object_info/CheckpointLoaderSimple"), timeout=aiohttp.ClientTimeout(total=8)) as r:
                    if r.status == 200:
                        data = await r.json()
                        return list(data.get("CheckpointLoaderSimple", {}).get("input", {}).get("required", {}).get("ckpt_name", [{}])[0].get("options", []))
        except Exception:
            pass
        return []

    async def generate(self, req: GenerationRequest) -> GenerationResult:
        if req.workflow:
            wf = json.loads(json.dumps(req.workflow))
        else:
            wf = json.loads(json.dumps(DEFAULT_COMFY_WORKFLOW))
            wf["6"]["inputs"]["text"] = req.prompt
            wf["7"]["inputs"]["text"] = req.negative_prompt
            wf["5"]["inputs"]["width"] = int(req.width)
            wf["5"]["inputs"]["height"] = int(req.height)
            wf["3"]["inputs"]["steps"] = int(req.steps)
            wf["3"]["inputs"]["cfg"] = float(req.cfg_scale)
            wf["3"]["inputs"]["seed"] = int(req.seed) if req.seed and req.seed >= 0 else int(time.time()) % 2**31
            if req.sampler:
                wf["3"]["inputs"]["sampler_name"] = req.sampler
            if req.model:
                wf["4"]["inputs"]["ckpt_name"] = req.model

        client_id = f"sd_draw-{int(time.time() * 1000)}"
        async with aiohttp.ClientSession() as s:
            async with s.post(
                self._url("/prompt"),
                json={"prompt": wf, "client_id": client_id},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                if r.status != 200:
                    raise BackendResultError(f"ComfyUI 提交失败 {r.status}: {(await r.text())[:500]}")
                data = await r.json()
            prompt_id = data.get("prompt_id")
            if not prompt_id:
                raise BackendResultError("ComfyUI 未返回 prompt_id")
            deadline = time.time() + self.cfg.backend_timeout
            img_bytes: bytes | None = None
            filename = ""
            while time.time() < deadline:
                await asyncio_sleep(2.0)
                try:
                    async with s.get(self._url("/history/" + prompt_id), timeout=aiohttp.ClientTimeout(total=8)) as rh:
                        if rh.status != 200:
                            continue
                        hist = await rh.json()
                except Exception:
                    continue
                if prompt_id not in hist:
                    continue
                entry = hist[prompt_id]
                if entry.get("status", {}).get("status_str") == "error":
                    msgs = entry.get("status", {}).get("messages", [])
                    raise BackendResultError(f"ComfyUI 出图报错: {json.dumps(msgs, ensure_ascii=False)[:500]}")
                outputs = entry.get("outputs", {})
                for node, out in outputs.items():
                    for img in out.get("images", []):
                        filename = img.get("filename", "")
                        subfolder = img.get("subfolder", "")
                        img_type = img.get("type", "output")
                        if filename:
                            qs = urllib.parse.urlencode({"filename": filename, "subfolder": subfolder, "type": img_type})
                            async with s.get(self._url("/view?" + qs), timeout=aiohttp.ClientTimeout(total=60)) as rv:
                                if rv.status == 200:
                                    img_bytes = await rv.read()
                    if img_bytes:
                        break
                if img_bytes:
                    break
            if not img_bytes:
                raise BackendResultError("ComfyUI 出图超时，未拿到图片")
            return GenerationResult(
                image_bytes=img_bytes,
                info={"filename": filename},
                backend=self.name,
            )


class OpenAIAdapter:
    """OpenAI 兼容生图 API：走 /images/generations，b64_json 直取。"""

    name = "openai"

    def __init__(self, cfg: SdDrawConfig):
        self.cfg = cfg

    def _base(self) -> str:
        return (self.cfg.openai_base_url or "https://api.openai.com/v1").rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.cfg.openai_api_key}"}

    @staticmethod
    def _size(w: int, h: int) -> str:
        """OpenAI 只认固定档位：方图 1024²，非方图按长宽比取横/竖档。"""
        w, h = int(w), int(h)
        if w <= 0 or h <= 0:
            return "1024x1024"
        if w > h:
            return "1792x1024"
        if h > w:
            return "1024x1792"
        return "1024x1024"

    async def health(self) -> bool:
        if not (self.cfg.openai_api_key or "").strip():
            return False
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    self._base() + "/models",
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as r:
                    return r.status == 200
        except Exception:
            return False

    async def generate(self, req: GenerationRequest) -> GenerationResult:
        if not (self.cfg.openai_api_key or "").strip():
            raise BackendResultError("OpenAI 密钥为空，先到管理页配置")
        model = (self.cfg.openai_model or "gpt-image-1").strip()
        payload = {
            "model": model,
            "prompt": req.prompt,
            "n": 1,
            "size": self._size(req.width, req.height),
            "response_format": "b64_json",
        }
        async with aiohttp.ClientSession() as s:
            async with s.post(
                self._base() + "/images/generations",
                headers=self._headers(),
                json=payload,
                timeout=aiohttp.ClientTimeout(total=self.cfg.backend_timeout),
            ) as r:
                if r.status != 200:
                    raise BackendResultError(
                        f"OpenAI images 返回 {r.status}: {(await r.text())[:500]}"
                    )
                data = await r.json()
        items = data.get("data") or []
        for it in items:
            if it.get("url"):
                async with aiohttp.ClientSession() as s2:
                    async with s2.get(it["url"], timeout=aiohttp.ClientTimeout(total=60)) as r2:
                        if r2.status == 200:
                            return GenerationResult(image_bytes=await r2.read(), info={}, backend=self.name)
            if it.get("b64_json"):
                b64 = it["b64_json"]
                try:
                    img_bytes = base64.b64decode(b64)
                except Exception as e:
                    raise BackendResultError(f"图片解码失败: {e}") from e
                return GenerationResult(
                    image_bytes=img_bytes, info={"model": model}, backend=self.name
                )
        raise BackendResultError("OpenAI 未返回图片数据（检查该模型是否支持生图）")


async def asyncio_sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)


async def backend_factory(cfg: SdDrawConfig) -> Any:
    """按配置实例化并做健康探测，主后端失联则按 webui → comfyui 顺序回退。"""
    order = [cfg.backend_primary, "webui", "comfyui"]
    seen: set[str] = set()
    for name in order:
        if name in seen:
            continue
        seen.add(name)
        if name == "openai":
            adapter: Any = OpenAIAdapter(cfg)
        elif name == "comfyui":
            adapter = ComfyUIAdapter(cfg)
        else:
            adapter = SDWebUIAdapter(cfg)
        if await adapter.health():
            return adapter
    # 全灭：优先返回主后端对象，让错误信息指向它
    if cfg.backend_primary == "openai":
        return OpenAIAdapter(cfg)
    if cfg.backend_primary == "comfyui":
        return ComfyUIAdapter(cfg)
    return SDWebUIAdapter(cfg)