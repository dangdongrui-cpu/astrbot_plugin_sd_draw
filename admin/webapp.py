"""sd_draw 管理页 API 路由（独立 aiohttp 服务，绕开面板鉴权）。

口令机制（需求拍板）：初始随机、打进日志、进页面改密。
所有 /api/* 请求需要 Header: X-Sd-Token 或 Cookie: sd_token。
浏览器直连 http://127.0.0.1:{admin_port}/ 即可进入管理台。
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import time
import urllib.parse
from pathlib import Path
from typing import Any

from aiohttp import web

from ..config import ConfigStore, DEFAULT_BADWORDS
from ..services.downloader import Downloader, resolve_download_url
from ..services.engine import EngineInstaller

STATIC_DIR = Path(__file__).parent / "static"

# 与 _conf_schema.json 保持同一份清单：管理页改动会同步进 AstrBot 面板配置，
# 让两边字段显示一致（面板重启后生效）。
PANEL_FIELDS = {
    "backend_primary", "webui_url", "comfyui_url",
    "openai_api_key", "openai_base_url", "openai_model",
    "default_model", "width", "height", "steps", "cfg_scale",
    "sampler", "negative_prompt", "hires", "hires_upscaler",
    "quota_per_user_per_day", "quota_total_per_day",
    "confirm_timeout", "cancel_timeout", "pollish_on", "pollish_model",
    "allow_private", "nsfw_on", "nsfw_whitelist",
    "download_dir", "download_source", "engine_dir", "admin_port",
}


# ---------------- aiohttp 适配层（旧 handler 零改动挂在独立端口上） ----------------
class _RequestProxy:
    """把 aiohttp 请求包成 handler 里习惯的 request 形态"""

    def __init__(self) -> None:
        self._req: web.Request | None = None

    def _bind(self, req: web.Request) -> None:
        self._req = req

    @property
    def headers(self):
        return self._req.headers

    @property
    def cookies(self):
        return self._req.cookies

    @property
    def query(self):
        return self._req.query

    async def json(self, default: Any = None):
        try:
            return await self._req.json()
        except Exception:
            return default


request = _RequestProxy()


def json_response(data: Any, status_code: int = 200, **kw) -> web.Response:
    return web.json_response(data, status=status_code, **kw)


def error_response(message: str, status_code: int = 400) -> web.Response:
    return web.json_response({"status": "error", "message": message}, status=status_code)


def file_response(path: Path) -> web.Response:
    return web.FileResponse(str(path))

# 内置推荐模型清单（都是各模型官方 repo 的直链，含镜像可换）
MODEL_CATALOG = [
    {
        "name": "动漫风（推荐）",
        "file": "animagine-xl-3.1.safetensors",
        "url": "https://huggingface.co/cagliostrolab/animagine-xl-3.1/resolve/main/animagine-xl-3.1.safetensors",
        "note": "~6.9GB，二次元 XL 模型（原 CounterfeitXL gated，已换 AnimagineXL3.1）",
    },
    {
        "name": "通用写实",
        "file": "sd_xl_base_1.0.safetensors",
        "url": "https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/resolve/main/sd_xl_base_1.0.safetensors",
        "note": "~6.9GB，SDXL 官方底座",
    },
    {
        "name": "轻量通用（1.5）",
        "file": "DreamShaper_8_pruned.safetensors",
        "url": "https://huggingface.co/Lykon/DreamShaper/resolve/main/DreamShaper_8_pruned.safetensors",
        "note": "~2GB，1.5 动画/写实通用",
    },
]

DEFAULT_STYLE_TABLE = {
    "styles": [
        {
            "name": "默认",
            "model": "",
            "positive": "",
            "negative": "",
            "params": {"steps": 28, "cfg_scale": 6.0, "sampler": "DPM++ 2M Karras"},
        },
        {
            "name": "动漫",
            "model": "animagine-xl-3.1.safetensors",
            "positive": "anime style, vibrant colors, detailed lineart",
            "negative": "",
            "params": {"steps": 28, "cfg_scale": 6.0, "sampler": "DPM++ 2M Karras"},
        },
        {
            "name": "写实",
            "model": "sd_xl_base_1.0.safetensors",
            "positive": "photorealistic, raw photo, natural lighting, 8k",
            "negative": "cartoon, anime, illustration",
            "params": {"steps": 32, "cfg_scale": 5.5, "sampler": "DPM++ 2M Karras"},
        },
        {
            "name": "水彩",
            "model": "",
            "positive": "watercolor painting, soft brush strokes, paper texture",
            "negative": "photorealistic, 3d render",
            "params": {"steps": 30, "cfg_scale": 7.5, "sampler": "Euler a"},
        },
    ]
}


class AdminApi:
    def __init__(self, cfg_store: ConfigStore, data_dir: Path, downloader: Downloader, installer: EngineInstaller, panel_cfg=None):
        self.cfg = cfg_store
        self.data_dir = data_dir
        self.downloader = downloader
        self.installer = installer
        self.panel_cfg = panel_cfg
        self.proc_file = data_dir / "engine_process.json"
        self.style_path = data_dir / "style_table.json"
        self.badwords_path = data_dir / "badwords.txt"
        self.presets_path = data_dir / "prompt_presets.json"
        self.model_presets_path = data_dir / "model_presets.json"
        self.dl_log_path = data_dir / "download_log.json"
        if not self.style_path.exists():
            self.style_path.parent.mkdir(parents=True, exist_ok=True)
            self.style_path.write_text(
                json.dumps(DEFAULT_STYLE_TABLE, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    # ---------- 基础 ----------
    def _authed(self) -> bool:
        tok = self.cfg.get().admin_token
        if not tok:
            return False
        header = request.headers.get("x-sd-token", "")
        cookie = urllib.parse.unquote(request.cookies.get("sd_token", ""))
        return bool(header == tok or cookie == tok)

    def _read_styles(self) -> dict[str, Any]:
        try:
            return json.loads(self.style_path.read_text(encoding="utf-8"))
        except Exception:
            return {"styles": []}

    # ---------- 下载记账 ----------
    def _dl_log(self, action: str, **kw) -> list[dict[str, Any]]:
        """下载记账小库：start 打点 / read 读取 / write 全量写 / deployed 标记部署。"""
        try:
            raw = json.loads(self.dl_log_path.read_text(encoding="utf-8"))
            records = raw.get("records", []) if isinstance(raw, dict) else []
        except Exception:
            records = []
        if action == "read":
            return records
        if action == "write":
            self.dl_log_path.write_text(
                json.dumps({"records": kw.get("records", records)}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return records
        if action == "start":
            records.insert(0, {
                "task_id": kw.get("task_id", ""),
                "file": kw.get("file", ""),
                "size": kw.get("size", 0),
                "time": time.strftime("%m-%d %H:%M", time.localtime()),
                "status": "running",
                "target": "",
                "error": "",
            })
            records = records[:100]
        elif action == "deployed":
            for rec in records:
                if rec.get("file") == kw.get("file") and not rec.get("target"):
                    rec["target"] = "已部署"
        self.dl_log_path.write_text(
            json.dumps({"records": records}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return records


    # ---------- 页面 ----------
    async def page_index(self):
        if not self._authed():
            return file_response(STATIC_DIR / "login.html")
        return file_response(STATIC_DIR / "index.html")

    # ---------- 登录 ----------
    async def check_token(self):
        if self._authed():
            return json_response({"ok": True})
        return error_response("口令不对，进不去哦", status_code=401)

    async def change_token(self):
        if not self._authed():
            return error_response("未授权", status_code=401)
        data = await request.json({})
        new_tok = (data or {}).get("token", "").strip()
        if len(new_tok) < 6:
            return error_response("新口令至少 6 位")
        self.cfg.save({"admin_token": new_tok})
        return json_response({"ok": True})

    # ---------- 配置 ----------
    async def get_config(self):
        if not self._authed():
            return error_response("未授权", status_code=401)
        cfg = self.cfg.get()
        d = cfg.to_dict()
        d["admin_token"] = "******" if d.get("admin_token") else ""
        return json_response({"config": d})

    async def save_config(self):
        if not self._authed():
            return error_response("未授权", status_code=401)
        data = (await request.json({})) or {}
        allowed = [
            "trigger", "aliases", "backend_primary", "webui_url", "comfyui_url",
            "openai_api_key", "openai_base_url", "openai_model",
            "backend_timeout", "default_model", "width", "height", "steps",
            "cfg_scale", "sampler", "negative_prompt", "seed",
            "quota_per_user_per_day", "quota_total_per_day", "desc_max_len",
            "confirm_timeout", "cancel_timeout", "pollish_on", "allow_private", "admin_port",
            "download_dir", "download_source", "custom_download_url", "hf_token", "engine_dir",
            "hires", "hires_upscaler", "hires_scale", "hires_denoise",
            "pollish_model", "nsfw_on", "nsfw_whitelist",
        ]
        patch = {k: v for k, v in data.items() if k in allowed and v is not None}
        self.cfg.save(patch)
        self._sync_panel_config(patch)
        return json_response({"ok": True})

    def _sync_panel_config(self, patch: dict) -> None:
        """把管理页改动同步进 AstrBot 面板配置，双账本合一（面板下次显示即此处最后值）。"""
        try:
            pc = self.panel_cfg
            if pc is None or not isinstance(pc, dict):
                return
            sub = {k: v for k, v in patch.items() if k in PANEL_FIELDS}
            if "nsfw_whitelist" in sub and isinstance(sub["nsfw_whitelist"], str):
                sub["nsfw_whitelist"] = [
                    x.strip() for x in sub["nsfw_whitelist"].replace("，", ",").replace("、", ",").split(",") if x.strip()
                ]
            if not sub:
                return
            pc.update(sub)
            if hasattr(pc, "save_config"):
                pc.save_config()
        except Exception as e:  # noqa: BLE001
            self._log(f"面板配置同步失败（不影响本页操作）：{e}")

    def _log(self, text: str) -> None:
        try:
            from ..main import logger  # 延迟导入，避免循环
            logger.info(f"[sd_draw] {text}")
        except Exception:
            print(f"[sd_draw] {text}")

    # ---------- 风格表 ----------
    async def get_styles(self):
        if not self._authed():
            return error_response("未授权", status_code=401)
        return json_response(self._read_styles())

    async def save_styles(self):
        if not self._authed():
            return error_response("未授权", status_code=401)
        data = (await request.json({})) or {}
        styles = data.get("styles")
        if not isinstance(styles, list) or not styles:
            return error_response("风格表不能为空")
        for s in styles:
            if not s.get("name"):
                return error_response("风格名不能为空")
        self.style_path.write_text(
            json.dumps({"styles": styles}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return json_response({"ok": True})

    # ---------- 禁词 ----------
    async def get_badwords(self):
        if not self._authed():
            return error_response("未授权", status_code=401)
        if not self.badwords_path.exists():
            return json_response({"words": []})
        return json_response({"words": [w for w in self.badwords_path.read_text(encoding="utf-8").splitlines() if w.strip() and not w.startswith("#")]})

    async def save_badwords(self):
        if not self._authed():
            return error_response("未授权", status_code=401)
        data = (await request.json({})) or {}
        words = data.get("words", [])
        if not isinstance(words, list):
            return error_response("格式不对")
        self.badwords_path.write_text(
            "\n".join(["# sd_draw 禁词表：每行一个词"] + [str(w).strip() for w in words]),
            encoding="utf-8",
        )
        return json_response({"ok": True})

    # ---------- 模型参数预设（每模型独立折叠页） ----------
    async def get_model_presets(self):
        if not self._authed():
            return error_response("未授权", status_code=401)
        try:
            raw = json.loads(self.model_presets_path.read_text(encoding="utf-8"))
            presets = raw.get("presets", []) if isinstance(raw, dict) else []
        except Exception:
            presets = []
        return json_response({"presets": presets})

    async def save_model_presets(self):
        if not self._authed():
            return error_response("未授权", status_code=401)
        data = (await request.json({})) or {}
        presets = data.get("presets")
        if not isinstance(presets, list):
            return error_response("格式不对")
        clean = []
        for p in presets:
            if not isinstance(p, dict):
                continue
            name = str(p.get("name") or "").strip()
            model = str(p.get("model") or "").strip()
            if not name or not model:
                continue
            clean.append({
                "name": name,
                "model": model,
                "width": int(p.get("width") or 0) or 0,
                "height": int(p.get("height") or 0) or 0,
                "steps": int(p.get("steps") or 0) or 0,
                "cfg_scale": float(p.get("cfg_scale") or 0) or 0.0,
                "sampler": str(p.get("sampler") or "").strip(),
                "negative_prompt": str(p.get("negative_prompt") or "").strip(),
                "hires": bool(p.get("hires")),
                "hires_upscaler": str(p.get("hires_upscaler") or "").strip(),
                "hires_scale": float(p.get("hires_scale") or 0) or 0.0,
                "hires_denoise": float(p.get("hires_denoise") or 0) or 0.0,
                "prompt_style": str(p.get("prompt_style") or "").strip(),
                "prompt_preset": str(p.get("prompt_preset") or "").strip(),
                "note": str(p.get("note") or "").strip(),
            })
        self.model_presets_path.parent.mkdir(parents=True, exist_ok=True)
        self.model_presets_path.write_text(
            json.dumps({"presets": clean}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return json_response({"ok": True, "count": len(clean)})

    # ---------- 润色提示词 ----------
    async def get_presets(self):
        if not self._authed():
            return error_response("未授权", status_code=401)
        from ..services.pollish import (POLLISH_SYSTEM, PROMPT_STYLE_EXTRA, STYLE_SYSTEM, TRANSLATE_SYSTEM, TWEAK_SYSTEM)
        def default_styles():
            return [
                {"name": "tag 流", "text": (PROMPT_STYLE_EXTRA.get("tag") or "").strip()},
                {"name": "长句流", "text": (PROMPT_STYLE_EXTRA.get("long") or "").strip()},
                {"name": "Pony 体系", "text": (PROMPT_STYLE_EXTRA.get("pony") or "").strip()},
            ]
        def defaults(name="默认预设"):
            return {
                "name": name,
                "pollish_system": POLLISH_SYSTEM,
                "style_system": STYLE_SYSTEM,
                "translate_system": TRANSLATE_SYSTEM,
                "tweak_system": TWEAK_SYSTEM,
                "styles": default_styles(),
            }
        if not self.presets_path.exists():
            row = defaults()
            self.presets_path.parent.mkdir(parents=True, exist_ok=True)
            self.presets_path.write_text(
                json.dumps({"active": row["name"], "presets": [row]}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return json_response({"presets": [row], "active": row["name"]})
        try:
            data = json.loads(self.presets_path.read_text(encoding="utf-8"))
        except Exception:
            return error_response("提示词文件解析失败", status_code=500)
        if not isinstance(data, dict):
            data = {}
        # 旧平铺结构 → 迁移为单条"默认预设"
        if not (isinstance(data.get("presets"), list) and data["presets"]):
            row = dict(data)
            row.setdefault("name", "默认预设")
            row = self._migrate_styles(row, default_styles)
            rows = [row]
            data = {"active": row["name"], "presets": rows}
            try:
                self.presets_path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
            return json_response({"presets": rows, "active": row["name"]})
        rows = [self._migrate_styles(r, default_styles) if isinstance(r, dict) else r
                for r in data["presets"]]
        if not data.get("active") or data["active"] not in [r.get("name") for r in rows if isinstance(r, dict)]:
            data["active"] = rows[0].get("name") if rows else ""
        return json_response({"presets": rows, "active": data["active"]})

    @staticmethod
    def _migrate_styles(row: dict, default_styles) -> dict:
        """把老 prompt_styles{tag,long,pony} 迁移成 styles[{name,text}]；缺失时补默认。"""
        if isinstance(row.get("styles"), list) and row["styles"]:
            return row
        styles = []
        old = row.pop("prompt_styles", None)
        if isinstance(old, dict):
            for key, label in (("tag", "tag 流"), ("long", "长句流"), ("pony", "Pony 体系")):
                v = old.get(key)
                if isinstance(v, str) and v.strip():
                    styles.append({"name": label, "text": v.strip()})
        if not styles:
            styles = default_styles()
        row["styles"] = styles
        return row

    async def save_presets(self):
        if not self._authed():
            return error_response("未授权", status_code=401)
        data = (await request.json({})) or {}
        rows = data.get("presets")
        if not isinstance(rows, list) or not rows:
            return error_response("至少保留一套预设")
        clean = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            name = (r.get("name") or "").strip()
            if not name:
                return error_response("预设名不能为空")
            item = {"name": name}
            for k in ("pollish_system", "style_system", "translate_system", "tweak_system"):
                v = r.get(k)
                item[k] = v.strip() if isinstance(v, str) else ""
            # 风格指令：命名数组，可增删；每条 name+text 必填
            styles = []
            for s in r.get("styles") or []:
                if isinstance(s, dict) and str(s.get("name") or "").strip():
                    styles.append({
                        "name": str(s.get("name")).strip(),
                        "text": str(s.get("text") or "").strip(),
                    })
            if styles:
                item["styles"] = styles
            clean.append(item)
        if not clean:
            return error_response("至少保留一套预设")
        active = (data.get("active") or clean[0]["name"]).strip()
        if active not in [c["name"] for c in clean]:
            active = clean[0]["name"]
        self.presets_path.parent.mkdir(parents=True, exist_ok=True)
        self.presets_path.write_text(
            json.dumps({"active": active, "presets": clean}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return json_response({"ok": True})

    # ---------- 下载 ----------
    async def list_models(self):
        if not self._authed():
            return error_response("未授权", status_code=401)
        cfg = self.cfg.get()
        files = await self.downloader.list_models(cfg)
        return json_response({"models": files, "catalog": MODEL_CATALOG})

    async def start_download(self):
        if not self._authed():
            return error_response("未授权", status_code=401)
        if self.downloader.running_tasks() >= 3:
            return error_response("同时下载的任务太多啦，最多 3 个")
        data = (await request.json({})) or {}
        stub = (data.get("file") or "").strip()
        raw_url = (data.get("url") or "").strip()
        if not stub and not raw_url:
            return error_response("要下载哪个文件？给文件名或直链都行")
        cfg = self.cfg.get()
        base_dir = Path(cfg.download_dir or (self.data_dir / "downloads"))
        try:
            usage = shutil.disk_usage(base_dir if base_dir.exists() else base_dir.parent)
            if usage.free < 2 * 1024**3:
                return error_response("目标盘剩余空间不足 2GB，先腾点地方再下载")
        except OSError:
            pass
        dest = base_dir / stub
        url = raw_url or resolve_download_url(
            next((m["url"] for m in MODEL_CATALOG if m["file"] == stub), ""),
            cfg.download_source, cfg.custom_download_url, stub,
        )
        if not url:
            return error_response("目录里没有这个模型，也没给直链")
        if dest.exists() and dest.stat().st_size > 0:
            return error_response("文件已存在，不用重复下")
        t = self.downloader.submit(url, str(dest), token=(cfg.hf_token or "").strip())
        self._dl_log("start", task_id=t.task_id, file=stub or dest.name, size=0)
        return json_response({"task_id": t.task_id})

    async def download_status(self):
        if not self._authed():
            return error_response("未授权", status_code=401)
        tasks = self.downloader.tasks()
        by_id = {t.get("task_id"): t for t in tasks}
        records = self._dl_log("read")
        changed = False
        for rec in records:
            t = by_id.get(rec.get("task_id"))
            if t:
                rec["status"] = t.get("status")
                rec["size"] = t.get("done") or 0
                rec["speed"] = t.get("speed") or 0
                rec["error"] = t.get("error") or ""
                changed = True
        if changed:
            self._dl_log("write", records=records)
        return json_response({"tasks": tasks, "records": records})

    async def cancel_download(self):
        if not self._authed():
            return error_response("未授权", status_code=401)
        data = (await request.json({})) or {}
        tid = (data.get("task_id") or "").strip()
        if self.downloader.cancel(tid):
            return json_response({"ok": True})
        return error_response("任务不存在或已结束")

    # ---------- 引擎 ----------
    async def engine_status(self):
        if not self._authed():
            return error_response("未授权", status_code=401)
        return json_response({"install": self.installer.status()})

    # ---------- 磁盘一览（下载/引擎目录就近选择） ----------
    async def disks(self):
        if not self._authed():
            return error_response("未授权", status_code=401)
        out = []
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            p = f"{letter}:\\"
            try:
                if os.path.exists(p):
                    out.append({"letter": letter, "path": p})
            except Exception:  # noqa: BLE001
                continue
        return json_response({"disks": out})

    # ---------- 引擎进程：一键启动 / 停止 / 状态 ----------
    async def engine_launch(self):
        if not self._authed():
            return error_response("未授权", status_code=401)
        data = (await request.json({})) or {}
        kind = (data.get("engine") or data.get("kind") or "webui").lower()
        if kind not in ("webui", "comfyui"):
            return error_response("engine 参数只支持 webui / comfyui")
        err = self.installer.launch(kind)
        if err:
            return error_response(err)
        pid = self.installer.last_pid()
        rec = {"kind": kind, "pid": pid, "started_at": time.time()}
        try:
            self.proc_file.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        return json_response({"ok": True, "pid": pid, "kind": kind,
                              "message": f"{'ComfyUI' if kind == 'comfyui' else 'WebUI(Forge)'} 正在拉起，新窗口里能看到启动日志；红绿灯变绿即就绪"})

    async def engine_stop(self):
        if not self._authed():
            return error_response("未授权", status_code=401)
        rec = None
        try:
            if self.proc_file.exists():
                rec = json.loads(self.proc_file.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            rec = None
        err = self.installer.stop(int(rec.get("pid") or 0) if rec else 0)
        if self.proc_file.exists():
            try:
                self.proc_file.unlink()
            except Exception:  # noqa: BLE001
                pass
        if err:
            return error_response(err)
        return json_response({"ok": True, "message": "已发送终止信号（含进程树）"})

    async def engine_process(self):
        if not self._authed():
            return error_response("未授权", status_code=401)
        rec = None
        try:
            if self.proc_file.exists():
                rec = json.loads(self.proc_file.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            rec = None
        return json_response({"process": rec})

    async def engine_install(self):
        if not self._authed():
            return error_response("未授权", status_code=401)
        err = self.installer.start()
        if err:
            return error_response(err)
        return json_response({"installed": True, "status": "后台安装中，页面会自动刷新进度"})

    # ---------- 打开引擎目录（本机资源管理器） ----------
    async def engine_open_dir(self):
        if not self._authed():
            return error_response("未授权", status_code=401)
        d = request.query.get("d", "") or ""
        if not d:
            return error_response("路径为空")
        p = Path(d)
        try:
            p.mkdir(parents=True, exist_ok=True)
            os.startfile(str(p))
        except Exception as e:  # noqa: BLE001
            return error_response(f"打开失败：{e}")
        return json_response({"ok": True})

    # ---------- 状态 ----------
    async def status(self):
        if not self._authed():
            return error_response("未授权", status_code=401)
        cfg = self.cfg.get()
        return json_response({
            "backend_primary": cfg.backend_primary,
            "webui_url": cfg.webui_url,
            "comfyui_url": cfg.comfyui_url,
            "default_model": cfg.default_model,
            "download_dir": cfg.download_dir or "(插件数据目录 downloads/)",
            "openai_base_url": cfg.openai_base_url,
            "openai_model": cfg.openai_model or "(未设)",
            "engine_dir": cfg.engine_dir or "(插件 data/engines/)",
        })

    # ---------- 引擎存活探测 ----------
    async def health(self):
        if not self._authed():
            return error_response("未授权", status_code=401)
        import aiohttp
        cfg = self.cfg.get()
        out = {"webui": False, "comfyui": False, "openai": False}
        async with aiohttp.ClientSession() as s:
            async def probe(url: str, path: str, headers=None) -> bool:
                try:
                    async with s.get(
                        url.rstrip("/") + path,
                        headers=headers or {},
                        timeout=aiohttp.ClientTimeout(total=4),
                    ) as r:
                        return r.status == 200
                except Exception:
                    return False

            out["webui"] = await probe(cfg.webui_url, "/sdapi/v1/options")
            out["comfyui"] = await probe(cfg.comfyui_url, "/system_stats")
            if (cfg.openai_api_key or "").strip():
                out["openai"] = await probe(
                    (cfg.openai_base_url or "https://api.openai.com/v1"),
                    "/models",
                    {"Authorization": f"Bearer {cfg.openai_api_key}"},
                )
        return json_response(out)

    # ---------- 选源抓列表（HF 搜索，gated 灰显） ----------
    async def models_search(self):
        if not self._authed():
            return error_response("未授权", status_code=401)
        data = (await request.json({})) or {}
        q = (data.get("search") or "").strip()
        if not q:
            return error_response("给个组织名或关键词，如 animagine 或 cagliostrolab")
        import aiohttp
        cfg = self.cfg.get()
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
        if (cfg.hf_token or "").strip():
            headers["Authorization"] = f"Bearer {cfg.hf_token.strip()}"
        api_base = "https://huggingface.co"
        if (cfg.download_source or "").strip() == "hf-mirror":
            api_base = "https://hf-mirror.com"
        try:
            async with aiohttp.ClientSession(headers=headers) as s:
                async with s.get(
                    api_base + "/api/models",
                    params={"search": q, "limit": 20, "expand": ["siblings"]},
                    timeout=aiohttp.ClientTimeout(total=12),
                ) as r:
                    if r.status != 200:
                        return error_response(f"HF API 返回 {r.status}，稍后再试")
                    items = await r.json()
        except Exception as e:
            return error_response(f"连接 HF API 失败：{e}")
        out = []
        for m in items or []:
            mid = m.get("modelId") or m.get("id") or ""
            gated = bool(m.get("gated")) or bool((m.get("cardData") or {}).get("gated"))
            files = [
                sib.get("rfilename", "")
                for sib in (m.get("siblings") or [])
                if sib.get("rfilename", "").endswith((".safetensors", ".ckpt"))
            ]
            out.append({
                "id": mid,
                "gated": gated,
                "files": files[:8],
            })
        return json_response({"ok": True, "models": out})

    # ---------- 一键部署：下载模型拷进引擎目录 ----------
    async def engine_deploy(self):
        if not self._authed():
            return error_response("未授权", status_code=401)
        data = (await request.json({})) or {}
        fname = (data.get("file") or "").strip()
        if not fname:
            return error_response("要部署哪个文件？")
        cfg = self.cfg.get()
        src_dir = Path(cfg.download_dir or (self.data_dir / "downloads"))
        src = src_dir / fname
        if not src.exists():
            return error_response(f"下载目录里没有 {fname}")
        eng_root = Path(cfg.engine_dir) if (cfg.engine_dir or "").strip() else (self.data_dir / "engines")
        # 递归扫描引擎根目录下的 models/Stable-diffusion（WebUI/Forge）与 models/checkpoints（ComfyUI）
        targets = []
        if eng_root.is_dir():
            for pat in ("models/Stable-diffusion", "models/checkpoints"):
                for sub in eng_root.rglob(pat):
                    if sub.is_dir():
                        targets.append(str(sub))
        targets = [Path(t) for t in dict.fromkeys(targets)]  # 去重后转回 Path
        if not targets:
            return error_response("引擎目录下没找到 models 子目录，先装引擎，或手动把文件放进引擎模型目录")
        for t in targets:
            shutil.copy2(src, t / src.name)
        self._dl_log("deployed", file=fname)
        return json_response({"ok": True, "targets": [str(t) for t in targets]})

    # ---------- 日志只读页 ----------
    async def logs(self):
        if not self._authed():
            return error_response("未授权", status_code=401)
        cfg = self.cfg.get()
        d = Path(cfg.log_dir) if (cfg.log_dir or "").strip() else (self.data_dir / "logs")
        files = sorted(d.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True) if d.exists() else []
        lines = []
        for f in files[:3]:
            txt = f.read_text(encoding="utf-8", errors="ignore")
            lines.append(f"===== {f.name} =====")
            lines.extend(txt.splitlines()[-120:])
        return json_response({"logs": chr(10).join(lines)})

    # ---------- 配置备份 / 恢复 ----------
    async def backup_config(self):
        if not self._authed():
            return error_response("未授权", status_code=401)
        bundle: dict[str, Any] = {}
        cfgf = self.data_dir / "config.json"
        if cfgf.exists():
            try:
                bundle["config"] = json.loads(cfgf.read_text(encoding="utf-8"))
            except Exception:
                bundle["config"] = {}
        for key, path in [("styles", self.style_path), ("badwords", self.badwords_path), ("presets", self.presets_path)]:
            if path.exists():
                bundle[key] = path.read_text(encoding="utf-8")
        return json_response({"bundle": bundle, "hint": "把 bundle 存好；恢复时原样传回来"})

    async def restore_config(self):
        if not self._authed():
            return error_response("未授权", status_code=401)
        data = (await request.json({})) or {}
        bundle = data.get("bundle") or {}
        if not isinstance(bundle, dict):
            return error_response("备份数据格式不对")
        n = 0
        if isinstance(bundle.get("config"), dict):
            self.cfg.save(bundle["config"])
            n += 1
        if isinstance(bundle.get("styles"), str):
            try:
                json.loads(bundle["styles"])
            except Exception:
                return error_response("styles 不是合法 JSON，拒绝恢复")
            self.style_path.write_text(bundle["styles"], encoding="utf-8")
            n += 1
        if isinstance(bundle.get("badwords"), str):
            self.badwords_path.write_text(bundle["badwords"], encoding="utf-8")
            n += 1
        if isinstance(bundle.get("presets"), str):
            try:
                json.loads(bundle["presets"])
            except Exception:
                return error_response("presets 不是合法 JSON，拒绝恢复")
            self.presets_path.write_text(bundle["presets"], encoding="utf-8")
            n += 1
        return json_response({"ok": True, "restored": n})

    # ---------- 独立服务（aiohttp 直连，绕开面板鉴权） ----------
    def build_app(self) -> web.Application:
        app = web.Application()
        routes = [
            web.get("/", self._wrap(self.page_index)),
            web.get("/login.html", self._wrap(self.page_index)),
            web.get("/index.html", self._wrap(self.page_index)),
            web.post("/api/check", self._wrap(self.check_token)),
            web.put("/api/token", self._wrap(self.change_token)),
            web.get("/api/config", self._wrap(self.get_config)),
            web.put("/api/config", self._wrap(self.save_config)),
            web.get("/api/styles", self._wrap(self.get_styles)),
            web.put("/api/styles", self._wrap(self.save_styles)),
            web.get("/api/badwords", self._wrap(self.get_badwords)),
            web.put("/api/badwords", self._wrap(self.save_badwords)),
            web.get("/api/presets", self._wrap(self.get_presets)),
            web.put("/api/presets", self._wrap(self.save_presets)),
            web.get("/api/modelpresets", self._wrap(self.get_model_presets)),
            web.put("/api/modelpresets", self._wrap(self.save_model_presets)),
            web.get("/api/models", self._wrap(self.list_models)),
            web.get("/api/download", self._wrap(self.download_status)),
            web.post("/api/download/start", self._wrap(self.start_download)),
            web.post("/api/download/cancel", self._wrap(self.cancel_download)),
            web.get("/api/engine/status", self._wrap(self.engine_status)),
            web.post("/api/engine/install", self._wrap(self.engine_install)),
            web.get("/api/engine/open", self._wrap(self.engine_open_dir)),
            web.get("/api/status", self._wrap(self.status)),
            web.get("/api/health", self._wrap(self.health)),
            web.post("/api/models/search", self._wrap(self.models_search)),
            web.post("/api/engine/deploy", self._wrap(self.engine_deploy)),
            web.get("/api/disks", self._wrap(self.disks)),
            web.post("/api/engine/launch", self._wrap(self.engine_launch)),
            web.post("/api/engine/stop", self._wrap(self.engine_stop)),
            web.get("/api/engine/process", self._wrap(self.engine_process)),
            web.get("/api/logs", self._wrap(self.logs)),
            web.get("/api/config/backup", self._wrap(self.backup_config)),
            web.post("/api/config/restore", self._wrap(self.restore_config)),
            web.static("/static", str(STATIC_DIR)),
        ]
        app.add_routes(routes)
        return app

    @staticmethod
    def _wrap(handler):
        async def _h(req: web.Request) -> web.Response:
            request._bind(req)
            try:
                resp = await handler()
                return resp
            except Exception as e:
                import traceback
                traceback.print_exc()
                return error_response(f"服务器开小差了：{type(e).__name__}: {e}", 500)

        return _h

    def run_standalone(self, host: str, port: int) -> None:
        """阻塞式启动独立管理页服务（务必放线程里跑）。"""
        web.run_app(self.build_app(), host=host, port=port)