"""
对话任务追踪器
记录每个被用户 @ 触发的回复任务的生命周期，供 Web 任务面板展示。
纯内存环形存储（默认 200 条），进程重启后清空。
"""

import threading
import time


class TaskTracker:
    """任务追踪器单例。"""

    _instance = None

    MAX_TASKS = 200

    # 状态 -> 中文标签（终态：replied/failed/skipped）
    STATUS_LABELS = {
        "pending": "排队等待",
        "context": "正在获取帖子上下文",
        "searching": "正在联网搜索",
        "generating": "正在生成回复",
        "publishing": "正在发布回复",
        "replied": "已回复",
        "failed": "回复失败",
        "skipped": "已跳过",
    }
    TERMINAL_STATUSES = ("replied", "failed", "skipped")

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._lock = threading.Lock()
        self._tasks = {}    # task_id -> dict
        self._order = []    # 创建顺序（旧 → 新）
        self._seq = 0
        self._by_msg = {}   # msg_id -> task_id（防重复创建）

    def create(self, msg_id, user_id, username, avatar, question) -> int:
        """创建任务，返回 task_id。同一 msg_id 重复调用返回已有任务 id。"""
        with self._lock:
            if msg_id and msg_id in self._by_msg:
                return self._by_msg[msg_id]
            self._seq += 1
            task_id = self._seq
            now = time.time()
            self._tasks[task_id] = {
                "id": task_id,
                "msg_id": msg_id,
                "user_id": str(user_id or ""),
                "username": username or "",
                "avatar": avatar or "",
                "question": question or "",
                "reply_text": "",
                "status": "pending",
                "status_label": self.STATUS_LABELS["pending"],
                "error": "",
                "created_at": now,
                "updated_at": now,
            }
            self._order.append(task_id)
            if msg_id:
                self._by_msg[msg_id] = task_id
            # 环形清理最旧任务
            while len(self._order) > self.MAX_TASKS:
                old_id = self._order.pop(0)
                old_task = self._tasks.pop(old_id, None)
                if old_task:
                    self._by_msg.pop(old_task.get("msg_id"), None)
            return task_id

    def update(self, task_id, status: str = None, reply_text: str = None,
               error: str = None, question: str = None):
        """更新任务状态/内容。终态之后到达的中间态回写会被忽略（状态机只前进）。"""
        if not task_id:
            return
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return
            if status and status in self.STATUS_LABELS:
                if task["status"] in self.TERMINAL_STATUSES and status not in self.TERMINAL_STATUSES:
                    pass  # 忽略终态后的中间态回写
                else:
                    task["status"] = status
                    task["status_label"] = self.STATUS_LABELS[status]
            if reply_text is not None:
                task["reply_text"] = reply_text
            if error is not None:
                task["error"] = error
            if question and not task["question"]:
                task["question"] = question
            task["updated_at"] = time.time()

    def list(self, keyword: str = "", uid: str = "", date: str = "",
             status: str = "", limit: int = 200) -> list:
        """按条件过滤（关键词命中提问/回复、UID 子串、创建日期、状态），最新在前。"""
        keyword = (keyword or "").strip().lower()
        uid = (uid or "").strip()
        with self._lock:
            tasks = [dict(t) for t in self._tasks.values()]
        try:
            limit = max(1, min(int(limit), 500))
        except (TypeError, ValueError):
            limit = 200

        result = []
        for t in tasks:
            if status and t["status"] != status:
                continue
            if uid and uid not in t["user_id"]:
                continue
            if keyword and keyword not in t["question"].lower() \
                    and keyword not in t["reply_text"].lower():
                continue
            if date and time.strftime("%Y-%m-%d", time.localtime(t["created_at"])) != date:
                continue
            result.append(t)
        result.sort(key=lambda t: t["created_at"], reverse=True)
        return result[:limit]


_tracker_instance = None


def get_task_tracker() -> TaskTracker:
    global _tracker_instance
    if _tracker_instance is None:
        _tracker_instance = TaskTracker()
    return _tracker_instance
