"""ComfyUI 一键安装：官方便携包下载 + 解压 + 依赖安装。

注意：WebUI 没有官方便携包，只能走源码 + 漫长陪跑，所以引擎安装
只支持 ComfyUI；WebUI 走管理页给出教程链接，让主人自己 git 流程装。
"""
from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import aiohttp

# 官方 Windows 便携包（含 python_embeded，解压即用）
COMFYUI_PORTABLE_URL = (
    "https://github.com/comfyanonymous/ComfyUI/releases/latest/download/"
    "ComfyUI_windows_portable_nvidia.7z"
)
# 7z 解压工具（Windows 自带 tar 解不了 7z，用 py7zr 兜底会太慢，优先找 7z.exe）
SEVENZIP_CANDIDATES = [
    r"C:\Program Files\7-Zip\7z.exe",
    r"C:\Program Files (x86)\7-Zip\7z.exe",
]


def find_7z() -> str:
    """保留字段：若 py7zr 不可用可回落到系统 7-Zip。"""
    for p in SEVENZIP_CANDIDATES:
        if Path(p).exists():
            return p
    return ""


def is_windows() -> bool:
    return platform.system() == "Windows"


class EngineInstaller:
    def __init__(self, install_dir: Path, cfg_getter=None):
        self._install_dir = install_dir
        self._cfg_getter = cfg_getter  # 可选：返回最新引擎根目录的 callable，运行中改 engine_dir 也认
        self._progress: dict = {
            "running": False,
            "step": "idle",
            "percent": 0,
            "message": "",
            "error": "",
            "started_at": 0.0,
        }
        self._lock = threading.Lock()

    def status(self) -> dict:
        with self._lock:
            return dict(self._progress)

    def is_running(self) -> bool:
        with self._lock:
            return bool(self._progress["running"])

    def start(self) -> str:
        """启动安装线程。返回立即错误消息或空串。"""
        if self.is_running():
            return "已有安装任务在跑"
        thread = threading.Thread(target=self._run, daemon=True)
        thread.start()
        return ""

    def _set(self, **kw: object) -> None:
        with self._lock:
            self._progress.update(kw)

    def _run(self) -> None:
        self._set(running=True, step="准备下载", percent=0, error="", started_at=time.time())
        try:
            asyncio.run(self._install())
        except Exception as e:
            self._set(running=False, step="error", error=str(e)[:400])
            return
        self._set(running=False, step="done", percent=100, message="安装完成")

    async def _install(self) -> None:
        self._root().mkdir(parents=True, exist_ok=True)
        archive = self._root() / "comfyui_portable.7z"
        self._set(step="下载便携包（约 1GB+，耐心）", percent=2)
        await self._download(COMFYUI_PORTABLE_URL, archive)
        self._set(step="解压中", percent=40)
        self._extract(archive, self._root())
        stale = self._root() / "ComfyUI_windows_portable"
        if not stale.exists():
            raise RuntimeError("解压后找不到 ComfyUI_windows_portable 目录")
        self._set(step="安装依赖", percent=75)
        py = stale / "python_embeded" / "python.exe"
        if py.exists():
            await self._run_cmd(str(py), ["-m", "pip", "install", "--no-input", "-U", "pip"])
            await self._run_cmd(str(py), ["-m", "pip", "install", "--no-input", "-r", str(stale / "ComfyUI" / "requirements.txt")])
        else:
            await self._run_cmd("python", ["-m", "pip", "install", "-r", str(stale / "ComfyUI" / "requirements.txt")])
        self._set(step="收尾", percent=95)
        archive.unlink(missing_ok=True)

    async def _download(self, url: str, dest: Path) -> None:
        tmp = dest.with_suffix(".part")
        headers = {"User-Agent": "Mozilla/5.0 sd_draw"}
        resume = tmp.stat().st_size if tmp.exists() else 0
        if resume:
            headers["Range"] = f"bytes={resume}-"
        async with aiohttp.ClientSession(trust_env=True) as s:
            async with s.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=None)) as r:
                if r.status not in (200, 206):
                    raise RuntimeError(f"下载失败 HTTP {r.status}")
                total = int(r.headers.get("Content-Length") or 0) + resume
                with open(tmp, "ab") as f:
                    async for chunk in r.content.iter_chunked(512 * 1024):
                        f.write(chunk)
                        if total:
                            self._set(percent=2 + 36 * (tmp.stat().st_size / total))
        os.replace(tmp, dest)

    def _extract(self, archive: Path, out: Path) -> None:
        import py7zr  # astrbot 运行时若没有则 fallback 下方提示

        with py7zr.SevenZipFile(archive, mode="r") as z:
            z.extractall(path=out)

    async def _run_cmd(self, exe: str, args: list[str]) -> None:
        proc = await asyncio.create_subprocess_exec(
            exe, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        await proc.communicate()

    # ================= 引擎进程：一键启动 / 停止 =================
    def _root(self) -> Path:
        """引擎根目录：优先现读配置，其次回落到构造时传入的目录。"""
        if callable(self._cfg_getter):
            try:
                r = self._cfg_getter()
                if r:
                    return Path(r)
            except Exception:
                pass
        return self._install_dir

    def _candidate_roots(self, kind: str) -> list[Path]:
        roots = [self._root()]  # engine_dir 直接指向引擎目录本身也认
        if kind == "comfyui":
            roots += [
                self._root() / "ComfyUI_windows_portable",
                self._root() / "ComfyUI",
                self._root() / "comfyui",
            ]
        elif kind == "webui":
            # WebUI/Forge：递归扫 webui-user.bat / webui.bat 定位（兼容 forge / -main 等任意目录名）
            root = self._root()
            roots = [root]
            if root.is_dir():
                for name in ("webui-user.bat", "webui.bat"):
                    for p in root.rglob(name):
                        roots.append(p.parent)
            roots = list(dict.fromkeys(roots))
        return roots

    def _find_launch(self, kind: str):
        """定位可启动脚本，返回 (script_path, work_dir) 或抛 RuntimeError。"""
        for root in self._candidate_roots(kind):
            if not root.is_dir():
                continue
            if kind == "comfyui":
                for name in ("run_nvidia_gpu.bat", "run_cpu.bat", "main.py"):
                    p = root / (name if name == "main.py" else name)
                    if name == "main.py":
                        p = root / "ComfyUI" / "main.py" if not p.exists() else p
                    if p.exists():
                        return p, root
                py = root / "python_embeded" / "python.exe"
                main = root / "ComfyUI" / "main.py"
                if py.exists() and main.exists():
                    # 用 python_embeded 直接起
                    return main, root
            else:
                # WebUI/Forge：只用 .bat（webui-user.bat 设 PYTHON 与 --api 后 call webui.bat；webui.bat 会激活 venv）
                # 绝不落到 launch.py 用系统 python 拉（Forge 依赖 venv）
                for name in ("webui-user.bat", "webui.bat"):
                    p = root / name
                    if p.exists():
                        return p, root
        raise RuntimeError(
            "没找到引擎启动脚本：请确认引擎目录（配置里的 engine_dir）下放了 Forge/WebUI 或 ComfyUI。"
            "Forge 目录里应有 webui.bat；ComfyUI 便携包应有 run_nvidia_gpu.bat。"
        )

    def _ensure_api_launcher(self, script: Path, root: Path) -> Path:
        """确保 bat 启动脚本带 --api，否则生成一个包了 COMMANDLINE_ARGS 的姊妹启动器。"""
        if not script.name.lower().endswith(".bat"):
            return script  # python 脚本自己传参就行
        try:
            text = script.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            text = ""
        if "--api" in text or "COMMANDLINE_ARGS" in text:
            return script
        launcher = root / "sd_draw_api_launcher.bat"
        nl = chr(13) + chr(10)
        launcher.write_text(
            "@echo off" + nl
            + "set COMMANDLINE_ARGS=--api" + nl
            + 'call "' + script.name + '"' + nl,
            encoding="ascii",
        )
        return launcher

    def launch(self, kind: str) -> str:
        """一键启动引擎，返回 pid 字符串或错误消息（非空=错误）。"""
        try:
            script, root = self._find_launch(kind)
            script = self._ensure_api_launcher(script, root)
            if script.name.lower().endswith(".py"):
                if kind == "comfyui":
                    py = root / "python_embeded" / "python.exe"
                    cmd = ([str(py), str(script)] if py.exists() else ["python", str(script)])
                else:
                    cmd = ["python", str(script)]
            else:
                # .bat 用 cmd /c 包裹 + 无窗口拉起，避免新 cmd 窗口一闪而过导致进程夭折
                cmd = ["cmd", "/c", str(script)]
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            CREATE_NO_WINDOW = 0x08000000
            proc = subprocess.Popen(
                cmd,
                cwd=str(root),
                creationflags=CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
            )
            self._last_pid = proc.pid
            return ""
        except RuntimeError as e:
            return str(e)
        except Exception as e:  # noqa: BLE001
            return f"启动失败：{e}"

    def last_pid(self) -> int:
        return int(getattr(self, "_last_pid", 0) or 0)

    def stop(self, pid: int) -> str:
        """终止进程树，返回错误消息或空串。"""
        if not pid:
            return "没有可停止的引擎进程记录"
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, timeout=20,
            )
            return ""
        except Exception as e:  # noqa: BLE001
            return f"停止失败：{e}"