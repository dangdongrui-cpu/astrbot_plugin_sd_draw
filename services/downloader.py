"""模型下载器：官方 / hf-mirror / 自定义 三源，断点续传 + 进度上报。

镜像策略：下载源的 host 直接替换为 hf-mirror.com / hf-mirror.co（huggingface.co 换 hf-mirror.com）。
管理页轮询 /api/plug/sd_draw/api/download 拿进度。
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp

HF_MIRRORS = ("hf-mirror.com", "hf-mirror.co", "hf-mirror.org")


def _status_hint(status: int) -> str:
    """给 HTTP 错误附一句人话，免得光秃秃一个状态码。"""
    hint = {
        401: "：仓库需登录/令牌（gated），自定义源或换模型",
        403: "：被拒绝访问，试试换下载源或稍后再来",
        404: "：文件不存在，换镜像或检查清单链接",
        429: "：下载太多被限流，稍等几分钟再试",
        502: "：源站/镜像网关抽风，换下载源（官方↔镜像）重试",
        503: "：源站过载，过会儿再试",
    }
    return hint.get(status, "")


@dataclass
class DownloadTask:
    task_id: str
    url: str
    dest: str
    total: int = 0
    done: int = 0
    status: str = "running"  # running | done | error | canceled
    error: str = ""
    speed: float = 0.0
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    token: str = ""
    """携带的 Bearer 令牌（解锁 gated 仓库），留空不带"""
    prompt_style: str = ""
    """提示词风格标记（tag/long/pony），透传给翻译器"""

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "url": self.url,
            "dest": Path(self.dest).name,
            "total": self.total,
            "done": self.done,
            "status": self.status,
            "error": self.error,
            "speed": round(self.speed, 2),
            "percent": round(self.done * 100 / self.total, 1) if self.total else 0.0,
        }


def _rewrite_to_mirror(url: str, mirror: str) -> str:
    if "huggingface.co" in url:
        return url.replace("huggingface.co", mirror)
    if "hf-mirror" in url:
        return url
    return url


class Downloader:
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 sd_draw/0.1"

    def __init__(self):
        self._tasks: dict[str, DownloadTask] = {}
        self._lock = threading.Lock()

    def task(self, task_id: str) -> DownloadTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    def tasks(self) -> list[dict[str, Any]]:
        with self._lock:
            return [t.to_dict() for t in self._tasks.values()]

    def running_tasks(self) -> int:
        with self._lock:
            return sum(1 for t in self._tasks.values() if t.status == "running")

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            t = self._tasks.get(task_id)
            if t and t.status == "running":
                t.status = "canceled"
                t.finished_at = time.time()
                return True
        return False

    def _register(self, t: DownloadTask) -> None:
        with self._lock:
            self._tasks[t.task_id] = t

    def submit(self, url: str, dest: str, token: str = "") -> DownloadTask:
        tid = uuid.uuid4().hex[:12]
        t = DownloadTask(task_id=tid, url=url, dest=dest, token=token or "")
        self._register(t)
        threading.Thread(target=self._run_loop, args=(t,), daemon=True).start()
        return t

    def _run_loop(self, t: DownloadTask) -> None:
        try:
            asyncio.run(self._download(t))
        except Exception as e:
            t.status = "error"
            t.error = str(e)[:300]
            t.finished_at = time.time()

    # 可自动重试的状态码：网关抽风 / 限流 / 服务端过载
    RETRYABLE_STATUS = (429, 500, 502, 503, 504)

    async def _download(self, t: DownloadTask) -> None:
        """带退避重试的下载壳：502/429/超时这类可恢复错误最多重试 2 次。"""
        for attempt in range(1, 4):
            if t.status == "canceled":
                return
            hard_err, soft_err = await self._attempt(t)
            if t.status == "canceled":
                return
            if not hard_err and not soft_err:
                return  # 成功或落下终态
            if soft_err and attempt < 3:
                t.status = "running"
                t.error = f"第 {attempt} 次失败（{soft_err}），{5 * attempt} 秒后重试…"
                await asyncio.sleep(5 * attempt)
                continue
            # 撞上硬错误或三连败，落终态
            t.status = "error"
            t.error = hard_err or soft_err or "下载失败"
            t.finished_at = time.time()
            return

    async def _attempt(self, t: DownloadTask) -> tuple[str, str]:
        """单次下载尝试。返回 (硬错误, 软错误)。硬错误=重试也白搭；软错误=可退避重试。"""
        dest = Path(t.dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        headers = {"User-Agent": self.USER_AGENT}
        if t.token:
            headers["Authorization"] = f"Bearer {t.token}"
        resume_from = tmp.stat().st_size if tmp.exists() else 0
        if resume_from:
            headers["Range"] = f"bytes={resume_from}-"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(t.url, headers=headers, timeout=aiohttp.ClientTimeout(total=None)) as r:
                    if r.status not in (200, 206):
                        hint = _status_hint(r.status)
                        msg = f"HTTP {r.status}{hint}"
                        if r.status in self.RETRYABLE_STATUS:
                            return "", msg
                        return msg, ""
                    t.total = int(r.headers.get("Content-Length") or 0) + resume_from
                    last = time.time()
                    with open(tmp, "ab") as f:
                        async for chunk in r.content.iter_chunked(256 * 1024):
                            if t.status == "canceled":
                                return "", ""
                            f.write(chunk)
                            t.done += len(chunk)
                            now = time.time()
                            if now - last >= 0.8:
                                t.speed = len(chunk) / (now - last) if now > last else 0.0
                                last = now
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            return "", str(e)[:300]
        if t.status == "canceled":
            return "", ""
        os.replace(tmp, dest)
        t.status = "done"
        t.total = t.done = t.total
        t.finished_at = time.time()
        return "", ""

    async def list_models(self, cfg: Any) -> list[dict[str, Any]]:
        """列出下载目录里的 .safetensors/.ckpt 文件。"""
        d = Path(cfg.download_dir or ".")
        out = []
        if d.exists():
            for f in sorted(d.glob("*.safetensors")) + sorted(d.glob("*.ckpt")):
                try:
                    out.append({"name": f.name, "size": f.stat().st_size})
                except OSError:
                    pass
        return out


def resolve_download_url(raw_url: str, source: str, custom_url: str, stub: str) -> str:
    """按下载源把 文件名/模型卡 转成最终直链。"""
    stub = stub.strip()
    if source == "custom" and custom_url:
        return custom_url.replace("{file}", stub)
    if source == "hf-mirror":
        return _rewrite_to_mirror(raw_url, "hf-mirror.com")
    if source == "hf-mirror2":
        return _rewrite_to_mirror(raw_url, "hf-mirror.co")
    return raw_url