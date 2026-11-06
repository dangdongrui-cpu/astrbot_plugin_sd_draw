"""配额管理：每人每天上限 + 全局每日上限，JSON 落盘。

规则（需求拍板）：
- 提交确认那一刻扣费（发起请求即扣，出图失败也扣）。
- 每人每天 1 张；全局每天 10 次。
- 跨天自动重置（按本地日期分桶）。
"""
from __future__ import annotations

import json
import threading
from datetime import date
from pathlib import Path
from typing import Any


class QuotaManager:
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

    def _save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def _bucket(self, today: date) -> dict[str, Any]:
        key = today.isoformat()
        b = self._data.get(key)
        if not isinstance(b, dict):
            b = {"users": {}, "total": 0}
            self._data[key] = b
        if not isinstance(b.get("users"), dict):
            b["users"] = {}
        if not isinstance(b.get("total"), int):
            b["total"] = 0
        return b

    def _cleanup(self, today: date) -> None:
        """只保留最近 7 天的桶，避免文件无限膨胀。"""
        keys = list(self._data.keys())
        cutoff = today.toordinal() - 7
        for k in keys:
            try:
                d = date.fromisoformat(k)
            except ValueError:
                continue
            if d.toordinal() < cutoff:
                del self._data[k]

    def used_by(self, user_id: str, today: date | None = None) -> int:
        with self._lock:
            today = today or date.today()
            return int(self._bucket(today)["users"].get(str(user_id), 0))

    def total_used(self, today: date | None = None) -> int:
        with self._lock:
            today = today or date.today()
            return int(self._bucket(today)["total"])

    def can_take(self, user_id: str, today: date | None = None) -> tuple[bool, str]:
        """是否还能发起一次。返回 (ok, 原因)。"""
        with self._lock:
            today = today or date.today()
            b = self._bucket(today)
            per_user = int(b["users"].get(str(user_id), 0))
            total = int(b["total"])
            self._cleanup(today)
            if per_user >= self._per_user_limit:
                return False, "per_user"
            if total >= self._total_limit:
                return False, "total"
            return True, ""

    # 限额由外部 config 注入
    _per_user_limit: int = 1
    _total_limit: int = 10

    def configure(self, per_user: int, total: int) -> None:
        with self._lock:
            self._per_user_limit = per_user if per_user > 0 else 1
            self._total_limit = total if total > 0 else 10

    def take(self, user_id: str, today: date | None = None) -> bool:
        """扣一次配额。返回是否成功扣下。"""
        with self._lock:
            today = today or date.today()
            ok, _ = self.can_take(user_id, today)
            if not ok:
                return False
            b = self._bucket(today)
            uid = str(user_id)
            b["users"][uid] = int(b["users"].get(uid, 0)) + 1
            b["total"] = int(b["total"]) + 1
            self._save()
            return True

    def summary(self, today: date | None = None) -> dict[str, Any]:
        with self._lock:
            today = today or date.today()
            b = self._bucket(today)
            return {
                "date": today.isoformat(),
                "per_user": self._per_user_limit,
                "total": self._total_limit,
                "users": dict(sorted(b["users"].items(), key=lambda x: -x[1])),
                "total_used": int(b["total"]),
            }