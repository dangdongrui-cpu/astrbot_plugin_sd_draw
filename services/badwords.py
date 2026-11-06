"""禁词过滤：原始描述与润色结果双重检查。

规则（需求拍板）：撞词打回重写；命中时不扣配额、不占用锁。
词表由管理页维护（data/badwords.txt，每行一个词，支持 # 注释）。
"""
from __future__ import annotations

import threading
from pathlib import Path


class BadWordsFilter:
    def __init__(self, path: Path, builtin: list[str] | None = None):
        self.path = path
        self._builtin = builtin or []
        self._lock = threading.RLock()
        self._words: list[str] = []
        self.load()

    def load(self) -> None:
        with self._lock:
            words: list[str] = []
            if self.path.exists():
                for line in self.path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    words.append(line)
            if not words:
                words = list(self._builtin)
            self._words = words

    def words(self) -> list[str]:
        with self._lock:
            return list(self._words)

    def set_words(self, words: list[str]) -> None:
        with self._lock:
            cleaned = [w.strip() for w in words if w and w.strip() and not w.startswith("#")]
            # 保留注释头说明
            lines = [
                "# sd_draw 禁词表：每行一个词，# 开头为注释",
                "# 原始描述与润色结果都会过此表",
            ] + cleaned
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("\n".join(lines), encoding="utf-8")
            self._words = cleaned

    def hit(self, text: str) -> str | None:
        """返回命中的第一个禁词，没有则 None。"""
        if not text:
            return None
        low = text.casefold()
        with self._lock:
            for w in self._words:
                if not w:
                    continue
                if w.casefold() in low:
                    return w
        return None