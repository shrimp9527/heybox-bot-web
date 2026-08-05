"""
日志管理器
内存环形缓冲区 + 文件持久化 + SSE 推送支持
线程安全写入，支持用户 ID 追踪和多线程标识
"""

import atexit
import os
import queue as queue_module
import threading
import time
from datetime import datetime
from collections import deque

MAX_BUFFER_SIZE = 2000  # 内存中最大保留日志条数

# 日志级别权重（低于配置级别的日志直接丢弃）
_LEVEL_ORDER = {"INFO": 20, "WARN": 30, "ERROR": 40}


class LogEntry:
    """日志条目"""
    def __init__(self, level: str, message: str, user_id: str = "", thread_name: str = ""):
        self.timestamp = datetime.now()
        self.level = level
        self.message = message
        self.user_id = user_id
        self.thread_name = thread_name or threading.current_thread().name

    def to_dict(self):
        return {
            "timestamp": self.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "level": self.level,
            "message": self.message,
            "user_id": self.user_id,
            "thread": self.thread_name,
        }

    def format_file(self):
        uid = f"[uid:{self.user_id}] " if self.user_id else ""
        return f"[{self.timestamp.strftime('%Y-%m-%d %H:%M:%S')}] [{self.level:5}] {uid}{self.message}"


class LogManager:
    """日志管理器单例（线程安全）"""

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
        self._buffer = deque(maxlen=MAX_BUFFER_SIZE)
        self._listeners = []  # SSE 监听器列表
        self._listener_lock = threading.Lock()  # 保护 _listeners 的注册/注销/遍历
        self._file_handle = None
        self._current_date = None
        self._min_level = _LEVEL_ORDER["INFO"]  # 低于该级别的日志直接丢弃
        self._last_flush = 0.0  # 上次文件 flush 时间（批量 flush 用）
        self._log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "log")
        self._write_lock = threading.Lock()  # 仅保护 buffer 写入 + 文件写入的原子性
        self._setup_file()
        atexit.register(self.close)

    def _setup_file(self):
        """设置日志文件（按日期分文件）。读取配置 log 段：path/level/max_day。"""
        try:
            # 局部 import 避免循环导入（config 加载时可能打日志）
            from config.config_manager import get_config
            log_cfg = get_config().get_raw("log")
        except Exception:
            log_cfg = {}

        # 日志根目录：配置 path（缺省 data/），相对项目根目录解析
        cfg_path = log_cfg.get("path", "data/") or "data/"
        project_root = os.path.dirname(os.path.dirname(__file__))
        self._log_dir = cfg_path if os.path.isabs(cfg_path) else os.path.join(project_root, cfg_path)

        # 日志级别：低于该级别的日志直接丢弃
        self._min_level = _LEVEL_ORDER.get(str(log_cfg.get("level", "INFO")).upper(), _LEVEL_ORDER["INFO"])

        # 清理超过 max_day 天的历史日志文件
        try:
            max_day = int(log_cfg.get("max_day", 7))
        except (ValueError, TypeError):
            max_day = 7
        self._cleanup_old_logs(max_day)

        try:
            os.makedirs(self._log_dir, exist_ok=True)
            today = datetime.now().strftime("%Y-%m-%d")
            self._current_date = today
            log_file = os.path.join(self._log_dir, f"bot_{today}.log")
            self._file_handle = open(log_file, "a", encoding="utf-8")
            self.info("日志系统初始化完成")
        except Exception as e:
            print(f"[LogManager] 无法打开日志文件: {e}")

    def _cleanup_old_logs(self, max_day: int):
        """启动时清理超过 max_day 天的 bot_*.log 文件。"""
        try:
            if not os.path.isdir(self._log_dir):
                return
            cutoff = time.time() - max_day * 86400
            for name in os.listdir(self._log_dir):
                if not (name.startswith("bot_") and name.endswith(".log")):
                    continue
                # 从文件名解析日期 bot_YYYY-MM-DD.log
                date_str = name[4:-4]
                try:
                    file_ts = time.mktime(time.strptime(date_str, "%Y-%m-%d"))
                except ValueError:
                    continue
                if file_ts < cutoff:
                    os.remove(os.path.join(self._log_dir, name))
        except Exception as e:
            print(f"[LogManager] 清理历史日志失败: {e}")

    def _rotate_file_if_needed(self):
        """如果日期变化或文件句柄关闭，切换/重开日志文件。"""
        today = datetime.now().strftime("%Y-%m-%d")
        if self._current_date == today and self._file_handle is not None:
            return
        # 关闭旧文件句柄（如有）
        try:
            if self._file_handle:
                self._file_handle.close()
        except Exception:
            pass
        try:
            log_file = os.path.join(self._log_dir, f"bot_{today}.log")
            self._file_handle = open(log_file, "a", encoding="utf-8")
            self._current_date = today
        except Exception as e:
            print(f"[LogManager] 无法切换日志文件: {e}")
            self._file_handle = None

    def info(self, message: str, user_id: str = ""):
        """记录信息日志。可附带 user_id 用于追踪。"""
        self._log("INFO", message, user_id)

    def warn(self, message: str, user_id: str = ""):
        """记录警告日志。可附带 user_id 用于追踪。"""
        self._log("WARN", message, user_id)

    def error(self, message: str, user_id: str = ""):
        """记录错误日志。可附带 user_id 用于追踪。"""
        self._log("ERROR", message, user_id)

    def _log(self, level: str, message: str, user_id: str = ""):
        """
        内部日志记录方法（线程安全）。
        仅对 buffer 写入 + 文件写入加锁；通知 listener 在锁外执行，
        避免 queue.put() 阻塞导致锁持有时间过长。
        """
        # 低于配置级别的日志直接丢弃
        if _LEVEL_ORDER.get(level, _LEVEL_ORDER["INFO"]) < self._min_level:
            return

        entry = LogEntry(level, message, user_id)

        # 原子化 buffer 写入 + 文件写入
        with self._write_lock:
            self._buffer.append(entry)
            self._rotate_file_if_needed()
            if self._file_handle:
                try:
                    self._file_handle.write(entry.format_file() + "\n")
                    # 批量 flush：距上次 flush 超过 1 秒或 ERROR 级别才立即落盘
                    now = time.time()
                    if level == "ERROR" or now - self._last_flush > 1:
                        self._file_handle.flush()
                        self._last_flush = now
                except Exception:
                    pass

        # 通知 SSE 监听器（锁外执行，避免阻塞写线程）
        self._notify_listeners(entry)

    def get_recent_logs(self, count: int = 500) -> list:
        """获取最近 N 条日志（倒序）。"""
        with self._write_lock:
            logs = list(self._buffer)
        logs.reverse()
        return [entry.to_dict() for entry in logs[:count]]

    def clear_logs(self):
        """清空日志缓冲区。"""
        with self._write_lock:
            self._buffer.clear()
        self.info("日志已清空")

    def export_logs(self) -> str:
        """导出日志内容：优先当日磁盘日志文件的完整内容，文件缺失时回退内存缓冲。"""
        log_file = os.path.join(self._log_dir, f"bot_{datetime.now().strftime('%Y-%m-%d')}.log")
        try:
            # 先 flush 保证缓冲区内容落盘，再读取完整文件
            with self._write_lock:
                if self._file_handle:
                    self._file_handle.flush()
            if os.path.exists(log_file):
                with open(log_file, "r", encoding="utf-8") as f:
                    return f.read()
        except Exception:
            pass
        with self._write_lock:
            logs = list(self._buffer)
        return "\n".join(entry.format_file() for entry in logs)

    def register_listener(self, queue):
        """注册 SSE 监听器队列。线程安全。"""
        with self._listener_lock:
            self._listeners.append(queue)

    def unregister_listener(self, queue):
        """注销 SSE 监听器队列。线程安全。"""
        with self._listener_lock:
            if queue in self._listeners:
                self._listeners.remove(queue)

    def _notify_listeners(self, entry: LogEntry):
        """通知所有监听器。使用 _listener_lock 保护列表遍历（快照模式）。
        队列已满（客户端消费不过来）时判定为死连接并移除。"""
        data = entry.to_dict()
        with self._listener_lock:
            listeners_snapshot = list(self._listeners)
        dead = []
        for listener_q in listeners_snapshot:
            try:
                listener_q.put_nowait(data)
            except queue_module.Full:
                dead.append(listener_q)
            except Exception:
                dead.append(listener_q)
        if dead:
            with self._listener_lock:
                for q in dead:
                    if q in self._listeners:
                        self._listeners.remove(q)

    def close(self):
        """关闭日志文件（flush 后关闭）。"""
        if self._file_handle:
            try:
                self._file_handle.flush()
            except Exception:
                pass
            self._file_handle.close()
            self._file_handle = None


# 全局实例
_log_instance = None


def get_logger() -> LogManager:
    global _log_instance
    if _log_instance is None:
        _log_instance = LogManager()
    return _log_instance
