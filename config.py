"""sd_draw 配置模型。

配置默认存放在插件安装目录下的 data/config.json，
管理页（/api/plug/sd_draw/）可直接读写，热生效。
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_BADWORDS = [
    "r18g", "loli", "shota", "child", "minor",
    "creepy", "guro", "scat", "gore", "nsfw",
]


@dataclass
class SdDrawConfig:
    # ---------- 基本 ----------
    trigger: str = "/draw"
    """主触发命令名（斜杠开头）"""
    aliases: list[str] = field(default_factory=lambda: ["画"])
    """触发命令别名（不带斜杠）"""

    # ---------- 后端 ----------
    backend_primary: str = "webui"
    """主后端：webui | comfyui | openai"""
    webui_url: str = "http://127.0.0.1:7860"
    """WebUI 地址（A1111 / Forge）"""
    comfyui_url: str = "http://127.0.0.1:8188"
    """ComfyUI 地址"""
    openai_api_key: str = ""
    """OpenAI 兼容 API 密钥（主后端选 openai 时必填）"""
    openai_base_url: str = "https://api.openai.com/v1"
    """OpenAI 兼容 API 基准地址（走中转站就填各自的 /v1）"""
    openai_model: str = "gpt-image-1"
    """OpenAI 生图模型名（官方 gpt-image-1；中转站可按品牌别名填）"""
    backend_timeout: int = 300
    """出图超时（秒）"""

    # ---------- 出图参数 ----------
    default_model: str = ""
    """默认 checkpoint（留空则用后端当前加载的模型）"""
    width: int = 1024
    height: int = 1216
    steps: int = 28
    cfg_scale: float = 6.0
    sampler: str = "DPM++ 2M Karras"
    negative_prompt: str = "lowres, bad anatomy, bad hands, extra fingers, blurry, worst quality, low quality, jpeg artifacts, watermark, text, cropped"
    seed: int = -1
    hires: bool = True
    """出图后自动高清修复（hires fix），分辨率拔高 + 边缘柔化"""
    hires_upscaler: str = "R-ESRGAN 4x+ Anime6B"
    """高清修复放大器（动漫模型选 Anime6B，线条柔中带细）"""
    hires_scale: float = 1.4
    """高清放大倍率（8GB 显存建议 1.4 以内，过大二遍采样易爆显存）"""
    hires_denoise: float = 0.4
    """二遍重绘强度（0.35~0.5，越高细节变化越大）"""

    # ---------- 策略 ----------
    quota_per_user_per_day: int = 1
    """每人每天可发起的图数（提交扣费时计数）"""
    quota_total_per_day: int = 10
    """全局每天可发起的图数"""
    desc_max_len: int = 500
    """描述最大字数，超出回兜底话术"""
    confirm_timeout: int = 30
    """菜单/确认等待秒数；超时走默认（不润色直接出图）"""
    cancel_timeout: int = 120
    """确认环节等待秒数；超时取消任务并解锁"""
    pollish_on: bool = True
    """润色功能总开关"""
    pollish_model: str = ""
    """润色/翻译/微调用模型（留空用当前默认模型）"""
    allow_private: bool = False
    """是否允许私聊使用"""
    nsfw_on: bool = False
    """NSFW 许可总闸（默认关=群聊命中敏感词就拒单；开=群里放行）。仅作用于群聊链路"""
    nsfw_whitelist: list[str] = field(default_factory=list)
    """NSFW 白名单群号（逗号分隔；白名单群即使总闸关闭也放行）"""

    # ---------- 下载 ----------
    download_dir: str = ""
    """模型下载目录（留空则默认后端模型目录）"""
    download_source: str = "hf-mirror"
    """下载源：official | hf-mirror | custom"""
    custom_download_url: str = ""
    """custom 源的自定义地址模板（{file} 会被替换成文件名）"""
    hf_token: str = ""
    """HuggingFace 令牌（解锁 gated/成人向仓库 401；抓列表和下载自动带上）"""
    engine_dir: str = ""
    """引擎安装/部署目录（留空则插件 data/engines；一键部署会把模型拷进其模型目录）"""

    # ---------- 管理 ----------
    admin_token: str = ""
    """管理页访问口令（初始随机生成并写入日志）"""
    admin_port: int = 7865
    """管理页独立端口（aiohttp 直连，绕开面板鉴权）"""

    # ---------- 日志 ----------
    log_dir: str = ""
    """出图与任务的日志目录（留空则插件 data/logs）"""

    @classmethod
    def defaults(cls) -> "SdDrawConfig":
        return cls()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SdDrawConfig":
        cfg = cls()
        if not data:
            return cfg
        for k, v in data.items():
            if hasattr(cfg, k) and v is not None:
                setattr(cfg, k, v)
        return cfg


class ConfigStore:
    """配置读写（带线程锁 + 落盘）。"""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        with self._lock:
            if self.path.exists():
                try:
                    self._data = json.loads(self.path.read_text(encoding="utf-8"))
                except Exception:
                    self._data = {}
            else:
                self._data = {}
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def get(self) -> SdDrawConfig:
        with self._lock:
            return SdDrawConfig.from_dict(self._data)

    def save(self, patch: dict[str, Any]) -> SdDrawConfig:
        with self._lock:
            self._data.update({k: v for k, v in patch.items() if v is not None})
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return SdDrawConfig.from_dict(self._data)