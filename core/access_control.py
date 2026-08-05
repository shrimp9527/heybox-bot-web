"""
访问控制模块
白名单模式 + 频率限制模式
"""

import time
import threading

from config.config_manager import get_config
from logger.log_manager import get_logger


class AccessControl:
    """访问控制单例"""

    _instance = None
    _singleton_lock = threading.Lock()  # 类级锁：仅保护单例创建；实例锁 _lock 保护调用计数数据

    def __new__(cls):
        if cls._instance is None:
            with cls._singleton_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._logger = get_logger()
        self._config = get_config()
        # {user_id: [(hour_key, count)]}
        self._user_call_times = {}
        self._lock = threading.Lock()  # 实例锁：保护 _user_call_times 读写
        self._cleanup_timer = None
        self._start_cleanup_timer()

    def _start_cleanup_timer(self):
        """启动每小时清理定时器。"""
        if self._cleanup_timer:
            self._cleanup_timer.cancel()
        # 计算到下一个整点的时间
        now = time.time()
        next_hour = (int(now // 3600) + 1) * 3600
        delay = next_hour - now + 1  # 整点过 1 秒执行
        self._cleanup_timer = threading.Timer(delay, self._hourly_cleanup)
        self._cleanup_timer.daemon = True
        self._cleanup_timer.start()

    def _hourly_cleanup(self):
        """每小时清零调用次数。"""
        with self._lock:
            self._user_call_times.clear()
        self._logger.info("所有用户调用次数统计已自动清零")
        # 继续下一轮定时
        self._start_cleanup_timer()

    def _current_hour_key(self) -> int:
        """获取当前小时的时间戳键值。"""
        return int(time.time() // 3600)

    def should_allow(self, user_id: str) -> dict:
        """
        检查是否允许回复该用户。
        返回: {"allowed": True/False, "reason": str, "details": dict}
        """
        bot_config = self._config.get_bot_config()
        mode = bot_config.get("mode", "white_list")

        if mode == "white_list":
            return self._check_whitelist(user_id, bot_config)
        elif mode == "frequency":
            return self._check_frequency(user_id, bot_config)
        else:
            # fail-closed：未知模式一律拒绝
            self._logger.warn(f"未知的访问控制模式: {mode}")
            return {"allowed": False, "reason": "未知的访问控制模式", "details": {}}

    def _check_whitelist(self, user_id: str, bot_config: dict) -> dict:
        """白名单模式检查。"""
        white_list = bot_config.get("white_list", [])
        try:
            uid_int = int(user_id)
        except (ValueError, TypeError):
            self._logger.warn(f"无法解析用户 ID: {user_id}")
            return {"allowed": False, "reason": f"用户 ID:{user_id} 格式无效", "details": {}}

        # 兼容手改配置为字符串的情况：元素逐个 int() 尝试，失败跳过
        white_ids = set()
        for item in white_list:
            try:
                white_ids.add(int(item))
            except (ValueError, TypeError):
                continue

        if uid_int in white_ids:
            return {"allowed": True, "reason": "", "details": {}}
        else:
            self._logger.info(f"用户 ID:{user_id} 不在白名单中，已忽略")
            return {"allowed": False, "reason": "不在白名单中", "details": {}}

    def _check_frequency(self, user_id: str, bot_config: dict) -> dict:
        """频率限制模式检查。"""
        max_calls = bot_config.get("frequency", 3)
        hour_key = self._current_hour_key()

        with self._lock:
            if user_id not in self._user_call_times:
                self._user_call_times[user_id] = []

            # 清理过期记录
            self._user_call_times[user_id] = [
                (hk, cnt) for hk, cnt in self._user_call_times[user_id]
                if hk == hour_key
            ]

            records = self._user_call_times[user_id]
            current_count = sum(cnt for _, cnt in records)

            if current_count >= max_calls:
                self._logger.warn(
                    f"用户 ID:{user_id} 本小时调用次数已用完 "
                    f"(已使用：{current_count} 次 / 上限：{max_calls} 次)，已忽略本次请求"
                )
                return {
                    "allowed": False,
                    "reason": f"本小时调用次数已用完 (已使用：{current_count} 次 / 上限：{max_calls} 次)",
                    "details": {"used": current_count, "limit": max_calls},
                }

            # 增加计数
            if records and records[-1][0] == hour_key:
                records[-1] = (hour_key, records[-1][1] + 1)
            else:
                records.append((hour_key, 1))

            new_count = sum(cnt for _, cnt in records)
            return {"allowed": True, "reason": "", "details": {"used": new_count, "limit": max_calls}}

    def get_usage_stats(self) -> dict:
        """获取当前使用统计。"""
        hour_key = self._current_hour_key()
        stats = {}
        with self._lock:
            for uid, records in self._user_call_times.items():
                cnt = sum(cnt for hk, cnt in records if hk == hour_key)
                if cnt > 0:
                    stats[uid] = cnt
        return stats

    def refund(self, user_id: str):
        """回滚一次频率配额（AI 回复/发布失败时调用）。
        非 frequency 模式直接返回；该用户当前小时有记录时最后一条计数减 1。"""
        bot_config = self._config.get_bot_config()
        if bot_config.get("mode", "white_list") != "frequency":
            return
        hour_key = self._current_hour_key()
        with self._lock:
            records = self._user_call_times.get(user_id)
            if not records:
                return
            hk, cnt = records[-1]
            if hk != hour_key:
                return
            if cnt <= 1:
                records.pop()
            else:
                records[-1] = (hk, cnt - 1)

    def stop(self):
        """停止定时器。"""
        if self._cleanup_timer:
            self._cleanup_timer.cancel()
            self._cleanup_timer = None


# 全局实例
_access_instance = None


def get_access_control() -> AccessControl:
    global _access_instance
    if _access_instance is None:
        _access_instance = AccessControl()
    return _access_instance