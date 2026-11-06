"""sd_draw —— 群聊本地 SD 生图插件（AstrBot v4）。

主流程：/draw 描述 → 禁词 → 抢锁 → 扣配额 → 四选一菜单
→（选风格则追问风格）→（润色则问 LLM 后贴回确认）→ 出图回群 → 解锁。

规则遵循需求拍板：
- 按 ID 抢锁只认第一人，忙时拒收不排队；
- 提交确认时扣配额，出图失败也照扣；
- 禁词双查（原始描述 + 润色结果），撞词打回重写；
- 30 秒菜单超时默认「直接」出图；确认环节超时取消任务并解锁；
- 管理页独立端口直连（默认 7865），口令初始随机打进日志，进页可改。
"""
from __future__ import annotations

import asyncio
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrbot.api.all import Star, Context, AstrMessageEvent, register, logger
from astrbot.api.event import filter
from astrbot.api.all import MessageChain

from .config import ConfigStore, DEFAULT_BADWORDS
from .services.quota import QuotaManager
from .services.badwords import BadWordsFilter
from .services.backend import backend_factory, GenerationRequest
from .services.pollish import Pollisher
from .services.downloader import Downloader
from .services.engine import EngineInstaller
from .admin.webapp import AdminApi


@dataclass
class Pending:
    group_id: str
    user_id: str
    user_name: str
    desc: str
    stage: str = "menu"          # menu | style | confirm | generating
    pollish: bool = False
    style_name: str = ""
    pollished: str = ""
    rewrite_count: int = 0
    created: float = field(default_factory=time.time)
    stage_since: float = field(default_factory=time.time)
    draw_event: AstrMessageEvent | None = None
    reply_event: AstrMessageEvent | None = None
    out_path: str = ""
    out_backend: str = ""
    out_error: str = ""


@register("astrbot_plugin_sd_draw", "dangdongrui-cpu", "群聊文生图插件：WebUI / ComfyUI / OpenAI 兼容接口，带润色确认、每日配额与管理页", "v0.1.0")
class SdDrawPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or None
        self.base_dir = Path(__file__).resolve().parent
        self.data_dir = self.base_dir / "data"
        self.cfg = ConfigStore(self.data_dir / "config.json")
        self._apply_panel_config()
        self.quota = QuotaManager(self.data_dir / "quota.json")
        self.badwords = BadWordsFilter(self.data_dir / "badwords.txt", DEFAULT_BADWORDS)
        self.downloader = Downloader()
        self.installer = EngineInstaller(
            self._resolve_engine_root(),
            cfg_getter=self._resolve_engine_root,
        )
        self.pollisher = Pollisher(
            context,
            presets=self._load_prompt_presets(),
            model=self.cfg.get().pollish_model,
            model_getter=lambda: self.cfg.get().pollish_model,
            prompt_styles=self._load_prompt_presets().get("prompt_styles"),
        )
        self.admin = AdminApi(self.cfg, self.data_dir, self.downloader, self.installer, panel_cfg=self.config)
        self._pending: dict[str, Pending] = {}
        self._busy = False
        self._pending_lock = asyncio.Lock()

        c = self.cfg.get()
        if not c.admin_token:
            tok = secrets.token_urlsafe(10)
            self.cfg.save({"admin_token": tok})
            logger.info(
                f"[sd_draw] 管理页口令已生成：{tok}"
                f"（页面 http://127.0.0.1:{{端口}}/，登录后请立即改口令）"
            )
        self._start_web_panel()
        self._timer_task = asyncio.get_event_loop().create_task(self._timer_loop())

    # ---------------- 管理页（独立 aiohttp 服务，绕开面板鉴权） ----------------
    def _start_web_panel(self) -> None:
        port = self._cfg().admin_port or 7865
        token = self._cfg().admin_token or ""
        host = "127.0.0.1"

        def serve() -> None:
            try:
                asyncio.set_event_loop(asyncio.new_event_loop())
                self.admin.run_standalone(host, port)
            except Exception as e:
                logger.error(f"[sd_draw] 管理页服务退出：{e}")

        t = threading.Thread(target=serve, daemon=True, name="sd_draw-web")
        t.start()
        logger.info(
            f"[sd_draw] 管理页已启动：http://{host}:{port}/ "
            f"（口令：{token}，登录后请立即在页面内改掉）"
        )

    # ---------------- 卸载清理 ----------------
    @filter.on_plugin_unloaded()
    async def _on_unload(self, metadata=None):
        try:
            self._timer_task.cancel()
        except Exception:
            pass

    # ---------------- 工具 ----------------
    def _apply_panel_config(self) -> None:
        """AstrBot 面板双轨配置：面板改动重启后作为初值下发，本地管理页保存的同名项随后可再覆盖。"""
        try:
            panel = getattr(self, "config", None)
            pdata = panel.data if hasattr(panel, "data") else (dict(panel) if isinstance(panel, dict) else {})
            if not isinstance(pdata, dict) or not pdata:
                return
            from .config import SdDrawConfig
            patch = {k: v for k, v in pdata.items() if hasattr(SdDrawConfig, k) and v is not None}
            # 面板对 list 型字段可能以文本形式保存，矫正类型
            for k in ("nsfw_whitelist",):
                if k in patch and isinstance(patch[k], str):
                    patch[k] = [x.strip() for x in patch[k].replace("，", ",").replace("、", ",").split(",") if x.strip()]
            if patch:
                self.cfg.save(patch)
                logger.info(f"[sd_draw] 已应用 AstrBot 面板配置 {len(patch)} 项（管理页可再微调）")
        except Exception as e:
            logger.debug(f"[sd_draw] 面板配置读取跳过：{e}")

    def _cfg(self):
        return self.cfg.get()

    def _resolve_engine_root(self) -> Path:
        c = self.cfg.get()
        return Path(c.engine_dir) if (c.engine_dir or "").strip() else (self.data_dir / "engines")

    def _load_prompt_presets(self) -> dict:
        try:
            raw = json.loads((self.data_dir / "prompt_presets.json").read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(raw, dict):
            return {}
        if isinstance(raw.get("presets"), list) and raw["presets"]:
            rows = [p for p in raw["presets"] if isinstance(p, dict) and p.get("name")]
            if not rows:
                return {}
            active = raw.get("active") or ""
            pick = next((p for p in rows if p.get("name") == active), rows[0])
            out = {k: v for k, v in pick.items() if k != "name"}
            return out
        return raw  # 旧平铺结构原样返回

    def _resolve_style_text(self, preset_name: str, style_name: str) -> str:
        """按 润色预设名 + 风格指令名 解析出指令文本。找不到返回空串。"""
        if not preset_name or not style_name:
            return ""
        try:
            data = json.loads((self.data_dir / "prompt_presets.json").read_text(encoding="utf-8"))
        except Exception:
            return ""
        rows = data.get("presets") if isinstance(data, dict) and isinstance(data.get("presets"), list) else []
        for p in rows:
            if not isinstance(p, dict) or p.get("name") != preset_name:
                continue
            for s in p.get("styles") or []:
                if isinstance(s, dict) and s.get("name") == style_name:
                    return str(s.get("text") or "")
        return ""

    def _load_styles(self) -> list[dict[str, Any]]:
        try:
            data = json.loads((self.data_dir / "style_table.json").read_text(encoding="utf-8"))
            return data.get("styles") or []
        except Exception:
            return []


    def _load_model_presets(self) -> list[dict]:
        """读模型参数预设表（data/model_presets.json），文件缺失/损坏返回空表。"""
        try:
            raw = json.loads((self.data_dir / "model_presets.json").read_text(encoding="utf-8"))
            return raw.get("presets", []) if isinstance(raw, dict) else []
        except Exception:
            return []

    def _send_text(self, p: Pending, text: str) -> asyncio.Task | None:
        ev = p.reply_event or p.draw_event
        if ev is None:
            return None
        task = asyncio.get_event_loop().create_task(ev.send(MessageChain().message(text)))

        def _swallow(exc: asyncio.Task) -> None:
            if not exc.cancelled() and exc.exception():
                logger.error(f"[sd_draw] 发送失败：{exc.exception()}")

        task.add_done_callback(_swallow)
        return task

    # ---------------- 主命令 ----------------
    @filter.command("draw", alias={"画"})
    async def draw_cmd(self, event: AstrMessageEvent):
        cfg = self._cfg()
        if event.is_private_chat() and not cfg.allow_private:
            yield event.plain_result("私聊暂不开放，去群里喊 /draw 哦")
            return
        text = event.message_str.strip()
        parts = text.split(maxsplit=1)
        args = parts[1].strip() if len(parts) > 1 else ""
        if not args:
            yield event.plain_result("请带上描述哦")
            return
        if len(args) > cfg.desc_max_len:
            yield event.plain_result(f"描述太长啦，精简到 {cfg.desc_max_len} 字以内再试")
            return
        gid = str(event.get_group_id())
        # NSFW 总闸：默认关（群聊命中敏感词即拒单）；开闸或白名单群放行；私聊不受此闸约束
        nsfw_ok = event.is_private_chat() or self._cfg().nsfw_on or gid in (self._cfg().nsfw_whitelist or [])
        if not nsfw_ok:
            hit = self.badwords.hit(args)
            if hit:
                yield event.plain_result(f"描述里有敏感内容（{hit}），换个说法再试")
                return
        uid = str(event.get_sender_id())
        async with self._pending_lock:
            if self._busy or gid in self._pending:
                yield event.plain_result("现在正忙着出图呢，稍等片刻再喊")
                return
            self.quota.configure(cfg.quota_per_user_per_day, cfg.quota_total_per_day)
            if not self.quota.take(uid):
                yield event.plain_result(f"今天的配额用完啦（每人每天 {cfg.quota_per_user_per_day} 张），明天再来")
                return
        p = Pending(group_id=gid, user_id=uid, user_name=event.get_sender_name() or "群友", desc=args, draw_event=event)
        async with self._pending_lock:
            self._pending[gid] = p
            self._busy = True
        logger.info(f"[sd_draw] {p.user_name}({uid}) 发起：{args[:50]}")
        yield event.plain_result(
            "请选择出图方式（30 秒不选默认 ③）：\n"
            "① 润色 + 默认风格\n② 润色 + 选风格\n③ 直接 + 默认风格\n④ 直接 + 选风格\n"
            "回复对应数字即可"
        ).use_markdown(False)

    # ---------------- 交互回复 ----------------
    @filter.regex(r"^\s*[0-9]+\s*$")
    async def menu_reply(self, event: AstrMessageEvent):
        gid = str(event.get_group_id())
        uid = str(event.get_sender_id())
        async with self._pending_lock:
            p = self._pending.get(gid)
            if not p or p.user_id != uid:
                return  # 没有匹配的流程，放行给其他插件
        if p.stage == "tweak":
            return  # 微调阶段的数字也交给 tweak_reply 裁决
        num = int(event.message_str.strip())
        cfg = self._cfg()

        if p.stage == "menu" and 1 <= num <= 4:
            p.reply_event = event
            if num in (1, 2):
                p.pollish = True
            if num in (2, 4):
                styles = self._load_styles()
                if len(styles) < 2:
                    p.style_name = styles[0]["name"] if styles else ""
                    await self._advance(gid, p, cfg)
                    return
                p.stage = "style"
                p.stage_since = time.time()
                lines = ["选个风格（30 秒不选直接用默认）："]
                for i, s in enumerate(styles, 1):
                    lines.append(f"{i}. {s['name']}")
                await event.send(MessageChain().message("\n".join(lines)))
                return
            await self._advance(gid, p, cfg)
            return

        if p.stage == "style":
            styles = self._load_styles()
            p.reply_event = event
            if 1 <= num <= len(styles):
                p.style_name = styles[num - 1]["name"]
            await self._advance(gid, p, cfg)
            return

        if p.stage == "confirm" and p.reply_event is not None:
            if num == 1:
                p.stage = "generating"
                await self._generate_and_send(gid, p, cfg)
            elif num == 2 and p.rewrite_count < 2:
                p.rewrite_count += 1
                await event.send(MessageChain().message("好，重写一版——"))
                await self._pollish_and_confirm(gid, p, cfg)
            elif num == 3:
                # 进入微调模式：直接发修改要求（文本指令），回 1 出图 / 4 取消
                p.stage = "tweak"
                p.stage_since = time.time()
                await event.send(MessageChain().message(
                    "微调模式开啦：直接发修改要求就行（例：删掉宝宝相关内容）。" + chr(10) +
                    "回 1 直接用当前稿出图，回 4 取消任务"
                ))
            elif num == 4:
                async with self._pending_lock:
                    self._pending.pop(gid, None)
                    self._busy = False
                await event.send(MessageChain().message("任务取消啦，配额不退哦（出图失败的规矩）"))
            else:
                await event.send(MessageChain().message("只能回 1（确认）/ 2（重写）/ 3（微调）/ 4（取消）哦"))

    @filter.regex(r"^[\s\S]+$")
    async def tweak_reply(self, event: AstrMessageEvent):
        """微调模式：任意文本当修改指令，数字 1 快捷出图 / 4 或"取消"退场。"""
        gid = str(event.get_group_id())
        uid = str(event.get_sender_id())
        async with self._pending_lock:
            p = self._pending.get(gid)
            if not p or p.user_id != uid or p.stage != "tweak":
                return  # 不是微调流程，放行给其他插件
        cfg = self._cfg()
        msg = event.message_str.strip()
        num = None
        try:
            num = int(msg)
        except ValueError:
            pass
        if num == 1:
            p.stage = "generating"
            await self._generate_and_send(gid, p, cfg)
            return
        if num == 4 or msg == "取消":
            async with self._pending_lock:
                self._pending.pop(gid, None)
                self._busy = False
            await event.send(MessageChain().message("任务取消啦，配额不退哦（出图失败的规矩）"))
            return
        if num in (2, 3):
            await event.send(MessageChain().message("当前是微调模式，直接发修改要求即可（如：删掉宝宝相关内容）"))
            return
        if not msg or len(msg) > 200:
            await event.send(MessageChain().message("修改要求太长啦，200 字以内哈，或者回 1 直接用当前稿"))
            return
        new_text = await self.pollisher.tweak(p.pollished or p.desc, msg)
        if not (self._cfg().nsfw_on or gid in (self._cfg().nsfw_whitelist or [])):
            hit = self.badwords.hit(new_text)
            if hit:
                await event.send(MessageChain().message(f"微调稿撞了禁词（{hit}），原稿先留着，换个说法再试"))
                return
        if new_text.strip() == (p.pollished or p.desc).strip():
            await event.send(MessageChain().message("这轮微调没变化？换个改法试试，回 1 也能直接用当前稿出图"))
            return
        p.pollished = new_text
        p.stage = "confirm"
        p.stage_since = time.time()
        await self._send_text(
            p,
            "微调稿来啦：" + chr(10) + "———" + chr(10) + new_text + chr(10) + "———" + chr(10) +
            f"回 1 确认出图 / 2 重写 / 3 微调修改 / 4 取消（{cfg.cancel_timeout} 秒不回应自动取消）",
        )
        return

    # ---------------- 流程推进 ----------------
    async def _advance(self, gid: str, p: Pending, cfg) -> None:
        if p.pollish:
            await self._pollish_and_confirm(gid, p, cfg)
        else:
            p.stage = "generating"
            await self._generate_and_send(gid, p, cfg)

    async def _pollish_and_confirm(self, gid: str, p: Pending, cfg) -> None:
        if not cfg.pollish_on or not await self.pollisher.available():
            logger.info("[sd_draw] 润色不可用，直接出图")
            p.stage = "generating"
            await self._generate_and_send(gid, p, cfg)
            return
        text = await self.pollisher.pollish(p.desc, p.style_name or None)
        if not (self._cfg().nsfw_on or gid in (self._cfg().nsfw_whitelist or [])):
            hit = self.badwords.hit(text)
            if hit:
                logger.info(f"[sd_draw] 润色结果命中禁词 {hit}，回退原始描述")
                text = p.desc
        p.pollished = text
        p.stage = "confirm"
        p.stage_since = time.time()
        await self._send_text(
            p,
            "润色好啦，验验货：\n———\n" + text + "\n———\n"
            f"回 1 确认出图 / 2 重写 / 3 微调修改 / 4 取消（{cfg.cancel_timeout} 秒不回应自动取消）",
        )

    async def _generate_and_send(self, gid: str, p: Pending, cfg) -> None:
        """出图耗时长，全部丢进后台任务，避免卡住对话响应。"""
        p.stage = "generating"
        self._send_text(p, "出图中，请稍候（最长几分钟）…")
        asyncio.get_event_loop().create_task(self._do_generate(gid, p, cfg))

    async def _do_generate(self, gid: str, p: Pending, cfg) -> None:
        err = ""
        try:
            backend = await backend_factory(cfg)
            prompt = p.pollished or p.desc

            # 模型参数预设：先按最终模型查预设，预设覆盖全局，再被风格 params 覆盖
            style = next((s for s in self._load_styles() if s["name"] == p.style_name), None)
            model = (style or {}).get("model") or cfg.default_model
            preset = next(
                (q for q in self._load_model_presets()
                 if q.get("model") == model or q.get("name") == model),
                None,
            ) if model else None
            params = {}
            if preset:
                for k in ("width", "height", "steps", "cfg_scale", "sampler", "negative_prompt",
                          "hires_upscaler", "hires_scale", "hires_denoise"):
                    if preset.get(k):
                        params[k] = preset[k]
                if preset.get("hires") is not None:
                    params["hires"] = bool(preset["hires"])
            params.update((style or {}).get("params") or {})

            # 提示词风格：模型预设 → 选润色预设 → 选该预设内的风格指令（可命名、可增删）
            style_text = ""
            prompt_style = (preset or {}).get("prompt_style") or ""
            if preset and preset.get("prompt_preset"):
                style_text = self._resolve_style_text(
                    preset.get("prompt_preset"), preset.get("prompt_style") or "")
                if style_text:
                    prompt_style = ""  # 已用指令文本，不再走档位 key
            if Pollisher.has_cjk(prompt):
                translated = await self.pollisher.translate(
                    prompt, prompt_style=prompt_style or None, style_text=style_text or None)
                if translated and translated != prompt and not Pollisher.has_cjk(translated):
                    prompt = translated
                    self._send_text(p, f"中文已自动英文化：{prompt}")
                else:
                    self._send_text(p, "提示词英文化失败，本次效果可能不佳，建议使用英文描述")

            pos = prompt
            extra_pos = (style or {}).get("positive") or ""
            if extra_pos:
                pos = f"{prompt}, {extra_pos}"
            neg = str(params.get("negative_prompt") or cfg.negative_prompt)
            extra_neg = (style or {}).get("negative") or ""
            if extra_neg:
                neg = f"{neg}, {extra_neg}" if neg else extra_neg
            req = GenerationRequest(
                prompt=pos,
                negative_prompt=neg,
                width=int(params.get("width") or cfg.width),
                height=int(params.get("height") or cfg.height),
                steps=int(params.get("steps") or cfg.steps),
                cfg_scale=float(params.get("cfg_scale") or cfg.cfg_scale),
                sampler=params.get("sampler") or cfg.sampler,
                seed=int(cfg.seed or -1),
                hires=params.get("hires", cfg.hires),
                hires_upscaler=params.get("hires_upscaler") or cfg.hires_upscaler,
                hires_scale=float(params.get("hires_scale") or cfg.hires_scale),
                hires_denoise=float(params.get("hires_denoise") or cfg.hires_denoise),
                model=model,
            )
            result = await backend.generate(req)
            tmp_dir = self.data_dir / "tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            out = tmp_dir / f"{gid}_{int(time.time())}.png"
            out.write_bytes(result.image_bytes)
            p.out_path = str(out)
            p.out_backend = result.backend
        except Exception as e:
            logger.error(f"[sd_draw] 出图失败：{e}")
            err = str(e)

        ev = p.reply_event or p.draw_event
        try:
            if err:
                chain = MessageChain().message(f"出图失败啦：{err[:200]}\n配额已按规矩扣除，重试请再喊 /draw")
            else:
                chain = MessageChain().file_image(p.out_path).message(f"[via {p.out_backend}]")
            await ev.send(chain)
        except Exception as e:
            logger.error(f"[sd_draw] 发送结果失败：{e}")
        finally:
            async with self._pending_lock:
                self._pending.pop(gid, None)
                self._busy = False

    # ---------------- 超时轮巡 ----------------
    async def _timer_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            try:
                cfg = self._cfg()
                now = time.time()
                for gid in list(self._pending):
                    p = self._pending[gid]
                    if p.stage in ("menu", "style") and now - p.stage_since > cfg.confirm_timeout:
                        logger.info(f"[sd_draw] 菜单超时，默认③直接出图（群 {gid}）")
                        p.stage = "generating"
                        await self._generate_and_send(gid, p, cfg)
                    elif p.stage in ("confirm", "tweak") and now - p.stage_since > cfg.cancel_timeout:
                        logger.info(f"[sd_draw] 确认超时，任务取消（群 {gid}）")
                        async with self._pending_lock:
                            self._pending.pop(gid, None)
                            self._busy = False
                        try:
                            await p.draw_event.send(MessageChain().message("等太久没回，任务取消啦（配额不退哦）"))
                        except Exception:
                            pass
            except Exception as e:
                logger.error(f"[sd_draw] 轮巡异常：{e}")