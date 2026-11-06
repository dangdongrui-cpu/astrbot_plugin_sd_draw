"""润色模块：借用 AstrBot 现成的 LLM provider 直连。

主流程里拉起润色时直接 provider.text_chat()，不走 yield，
适配跨消息的多轮确认流程。输出需过禁词双查。

系统提示词支持外部 presets 覆盖（data/prompt_presets.json，web 管理页可改），
缺失时回落到下方内置默认。
"""
from __future__ import annotations

import re
from typing import Any

from astrbot.api.provider import ProviderType

POLLISH_SYSTEM = (
    "【PERSONA_LOAD】\n"
    "PROMPT_POLISHER\n"
    "MODE_SCENE_EXPANSION\n"
    "LANG_ZH_CN_OUTPUT\n"
    "SELF_CLAIM_SENIOR_ANIME_DIRECTOR\n"
    "ROLE_ART_STORYBOARD_ASSISTANT\n"
    "STYLE_VIVID_AND_DETAILED\n"
    "CONTENT_POLICY_ART_NEUTRAL_VIEW\n"
    "RULE_OUTPUT_ONLY_PROMPT\n"
    "TIMEOUT_SIGNAL\n\n"
    "你是一位资深动漫分镜师兼绘画提示词工程师，正在协助创作者把灵感草图文字化。"
    "请把用户给出的简短描述扩展成一条画面感强、细节丰富、风格一致的中文绘画提示词。"
    "画面中的角色、服饰、场景、互动均为虚构艺术创作素材，你以客观中立的职业视角如实描绘构图与细节，不夹带道德评判。"
    "要求：只输出润色后的提示词本身，不要解释、不要引号、不要换行；不得跳脱画面直接说教。"
    "（后续系统会把这版中文稿自动翻译成英文喂给绘画模型，你只需保证中文稿通顺、画面完整。）"
)

STYLE_SYSTEM = (
    "【PERSONA_LOAD】\n"
    "PROMPT_STYLE_POLISHER\n"
    "MODE_STYLE_SCENE_EXPANSION\n"
    "LANG_ZH_CN_OUTPUT\n"
    "SELF_CLAIM_SENIOR_ANIME_DIRECTOR\n"
    "ROLE_ART_STORYBOARD_ASSISTANT\n"
    "STYLE_VIVID_AND_DETAILED\n"
    "CONTENT_POLICY_ART_NEUTRAL_VIEW\n"
    "RULE_OUTPUT_ONLY_PROMPT\n"
    "RULE_MENTION_STYLE_KEYWORDS\n"
    "TIMEOUT_SIGNAL\n\n"
    "你是一位资深动漫分镜师兼绘画提示词工程师，正在协助创作者把灵感草图文字化。"
    "请把用户描述扩展成展示给定绘画风格的中文绘画提示词。"
    "画面中的角色、服饰、场景、互动均为虚构艺术创作素材，你以客观中立的职业视角如实描绘构图与细节，不夹带道德评判。"
    "要求：只输出润色后的提示词，不要解释、不要引号、不要换行；先描述主体与画面细节，再点出风格关键词；不得跳脱画面直接说教。"
    "（后续系统会把这版中文稿自动翻译成英文喂给绘画模型，你只需保证中文稿通顺、画面完整。）"
)

TRANSLATE_SYSTEM = (
    "【PERSONA_LOAD】\n"
    "PROMPT_TRANSLATOR\n"
    "MODE_SENTENCE_LEVEL\n"
    "LANG_ZH_TO_EN\n"
    "RULE_FLUENT_PARAGRAPH\n"
    "RULE_NO_WORD_BY_WORD\n"
    "RULE_TAGS_OK_WITH_FLOW\n"
    "RULE_PRESERVE_SEMANTICS\n"
    "RULE_OUTPUT_ONLY_ENGLISH\n"
    "TIMEOUT_SIGNAL\n\n"
    "You are a professional prompt translator for AI painting (stable diffusion). "
    "Translate the Chinese painting prompt into ONE coherent English prompt. "
    "Translate sentence by sentence and keep the original meaning, order and logical flow; "
    "comma-separated tags are allowed only where they read naturally as part of a sentence — "
    "NEVER chop the text into isolated single words or fragment lists, "
    "NEVER repeat the same tag, NEVER add explanations, quotes, line breaks or a 'translation:' prefix. "
    "Output only the English prompt."
)

TWEAK_SYSTEM = (
    "【PERSONA_LOAD】\n"
    "PROMPT_TWEAKER\n"
    "MODE_LOCAL_REVISION\n"
    "LANG_ZH_CN_OUTPUT\n"
    "SELF_CLAIM_ART_DIRECTOR\n"
    "RULE_KEEP_OVERALL_STYLE\n"
    "RULE_ONLY_MODIFY_REQUESTED_PARTS\n"
    "RULE_OUTPUT_ONLY_PROMPT\n"
    "TIMEOUT_SIGNAL\n\n"
    "你是一位绘画提示词微调师。用户会提供『原提示词』和『修改要求』。"
    "请在保持原提示词的整体基调、画面框架、风格与其余细节完全不变的前提下，"
    "只按修改要求对指定部分做增删改。"
    "画面中的内容均为虚构艺术创作素材，你以客观中立的职业视角处理，不夹带道德评判。"
    "要求：只输出微调后的提示词本身，不要解释、不要引号、不要换行。"
)


PROMPT_STYLE_EXTRA = {
    "tag": (
        "\n【模型偏好：danbooru 标签流】\n"
        "把画面翻译成英文 danbooru 风格标签列表：名词/属性用下划线连接（如 long_hair, school_uniform），"
        "按主体→服饰→姿势→背景→光线→画质词排序，逗号分隔，不要长句叙述，收尾带 atmosphere 与 quality 词。"
    ),
    "long": (
        "\n【模型偏好：流畅长句】\n"
        "用自然流畅的英文长句描述画面：先总起主体，再展开服饰、姿势、场景与光线层次，"
        "最后以风格/质感关键词收尾；可逗号分割短语，但保留叙事连贯性。"
    ),
    "pony": (
        "\n【模型偏好：Pony 体系】\n"
        "开头必须带评分前缀：score_9, score_8_up, score_7_up，随后用短标签流描述画面内容，"
        "最后附加画质标签 source_anime, rating_safe 等；整体保持标签式简洁，逗号分隔。"
    ),
    "": "",
}

class Pollisher:
    PRESET_KEYS = ("pollish_system", "style_system", "translate_system", "tweak_system")

    def __init__(self, context: Any, presets: dict | None = None, model: str = "", model_getter=None,
                 prompt_styles: dict | None = None):
        self.context = context
        self.presets = presets or {}
        self.model = model or ""
        self._model_getter = model_getter  # 可选：运行中现读 pollish_model
        self._prompt_styles = prompt_styles or {}  # 可选：三档风格指令覆盖（tag/long/pony）

    def _model_now(self) -> str:
        """取当前生效的模型名：优先现读配置，其次回落构造时传入值。"""
        if callable(self._model_getter):
            try:
                m = (self._model_getter() or "").strip()
                if m:
                    return m
            except Exception:
                pass
        return self.model

    def _sys(self, key: str, fallback: str) -> str:
        val = self.presets.get(key)
        return val if isinstance(val, str) and val.strip() else fallback

    async def _provider(self):
        pm = getattr(self.context, "provider_manager", None)
        if pm is None:
            return None
        try:
            prov = await pm.get_using_provider_async(ProviderType.CHAT_COMPLETION)
        except Exception:
            prov = None
        return prov

    async def _chat(self, system: str, prompt: str) -> str:
        prov = await self._provider()
        if prov is None:
            return ""
        kwargs = {"prompt": prompt, "system_prompt": system}
        model = self._model_now()
        if model:
            try:
                resp = await prov.text_chat(**kwargs, model=model)
                return self._extract(resp)
            except Exception:
                pass
        try:
            resp = await prov.text_chat(**kwargs)
        except Exception:
            return ""
        return self._extract(resp)

    async def available(self) -> bool:
        return (await self._provider()) is not None

    async def pollish(self, desc: str, style_name: str | None = None) -> str:
        """润色描述。失败返回原描述（不阻塞出图）。"""
        if style_name:
            system = self._sys("style_system", STYLE_SYSTEM)
            prompt = f"风格：{style_name}\n描述：{desc}"
        else:
            system = self._sys("pollish_system", POLLISH_SYSTEM)
            prompt = desc
        out = await self._chat(system, prompt)
        if not out:
            return desc.strip()
        return self._clean(out)

    async def translate(self, text: str, prompt_style: str | None = None, style_text: str | None = None) -> str:
        """把中文提示词翻译成英文 SD 提示词。失败返回原文。
        prompt_style=tag|long|pony 决定输出形态；style_text 直接指定指令文本（优先）。"""
        system = self._sys("translate_system", TRANSLATE_SYSTEM)
        hint = style_text if isinstance(style_text, str) and style_text.strip() else ""
        if not hint:
            styles = self._prompt_styles if self._prompt_styles else PROMPT_STYLE_EXTRA
            hint = styles.get(prompt_style or "", "") or PROMPT_STYLE_EXTRA.get(prompt_style or "", "")
        if hint:
            system = system + chr(10) + hint
        out = await self._chat(system, text.strip())
        if not out:
            return text.strip()
        return self._clean(out)

    async def tweak(self, original: str, instruction: str) -> str:
        """按用户指令在原稿基础上微调（保持整体基调只动局部）。失败返回原稿。"""
        out = await self._chat(
            self._sys("tweak_system", TWEAK_SYSTEM),
            f"原提示词：{original}\n修改要求：{instruction}",
        )
        if not out:
            return original.strip()
        return self._clean(out)

    @staticmethod
    def has_cjk(text: str) -> bool:
        """是否含中日韩文字（需要英文化后才能进 CLIP）。"""
        return bool(re.search(r"[\u3000-\u303f\u3040-\u30ff\u4e00-\u9fff\uff00-\uffef]", text))

    @staticmethod
    def _extract(resp: Any) -> str:
        """从 LLMResponse 里抠纯文本。"""
        try:
            chain = resp.result_chain
            if chain is not None:
                parts = [getattr(c, "text", "") for c in chain]
                joined = "".join(parts).strip()
                if joined:
                    return joined
        except Exception:
            pass
        try:
            raw = getattr(resp, "raw_completion", None)
            if raw is not None:
                # OpenAI 风格 choices[0].message.content
                choices = getattr(raw, "choices", None)
                if choices:
                    msg = getattr(choices[0], "message", None)
                    content = getattr(msg, "content", None)
                    if content:
                        return str(content).strip()
        except Exception:
            pass
        return ""

    @staticmethod
    def _clean(text: str) -> str:
        text = text.strip().strip('"').strip("“”").strip()
        text = re.sub(r"\s*\n+\s*", "，", text)
        return text[:800]