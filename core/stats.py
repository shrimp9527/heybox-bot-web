"""
统计管理器
追踪所有运行数据：触发次数、成功/失败、搜索、Token 消耗
"""

import threading
from typing import Dict


class StatsManager:
    """统计管理器单例"""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        """重置所有统计。"""
        with self._lock:
            self._trigger_count = 0       # 触发回复次数
            self._reply_success = 0       # 回复成功
            self._reply_fail = 0          # 回复失败
            self._search_count = 0        # 搜索调用次数
            self._total_tokens = 0        # Token 总消耗
            self._model_stats: Dict[str, Dict] = {}  # {模型类型: {model: str, tokens: int}}
            self._steam_ratings: Dict[str, int] = {"SSS": 0, "SS": 0, "S": 0, "A": 0, "B": 0, "C": 0, "D": 0}

    def record_trigger(self):
        """记录一次触发。"""
        with self._lock:
            self._trigger_count += 1

    def record_reply_success(self):
        """记录一次成功回复。"""
        with self._lock:
            self._reply_success += 1

    def record_reply_fail(self):
        """记录一次失败回复。"""
        with self._lock:
            self._reply_fail += 1

    def record_search(self):
        """记录一次搜索调用。"""
        with self._lock:
            self._search_count += 1

    def record_llm_call(self, label: str, model: str, tokens: int):
        """
        记录一次 LLM 调用产生的 token 消耗。
        label: 模型类型标签（"回复模型"/"搜索模型"等）
        """
        with self._lock:
            self._total_tokens += tokens
            if label not in self._model_stats:
                self._model_stats[label] = {"model": model, "tokens": 0, "calls": 0}
            self._model_stats[label]["model"] = model
            self._model_stats[label]["tokens"] += tokens
            self._model_stats[label]["calls"] = self._model_stats[label].get("calls", 0) + 1

    def record_steam_rating(self, rating: str):
        """记录一次 Steam 库存评级。"""
        rating = rating.strip().upper()
        if rating in self._steam_ratings:
            with self._lock:
                self._steam_ratings[rating] += 1

    def get_stats(self) -> dict:
        """获取当前统计数据。"""
        with self._lock:
            # 深拷贝 model_stats，避免外部修改内部状态
            model_stats = {label: dict(s) for label, s in self._model_stats.items()}
            return {
                "trigger_count": self._trigger_count,
                "reply_success": self._reply_success,
                "reply_fail": self._reply_fail,
                "search_count": self._search_count,
                "total_tokens": self._total_tokens,
                "model_stats": model_stats,
                "steam_ratings": dict(self._steam_ratings),
            }


# 全局实例
_stats_instance = None


def get_stats() -> StatsManager:
    global _stats_instance
    if _stats_instance is None:
        _stats_instance = StatsManager()
    return _stats_instance