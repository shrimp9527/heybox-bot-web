"""
会话管理器
负责小黑盒登录会话的持久化、检测、恢复
"""

import os
import threading
import time

from . import heybox_api
from .json_store import atomic_write_json, load_json_with_backup
from logger.log_manager import get_logger


SESSION_FILE = "session.json"


class SessionManager:
    """小金盒会话管理器单例"""

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
        self._logger = get_logger()
        self._data_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
        self._session_path = os.path.join(self._data_dir, SESSION_FILE)

        self.heybox_id = ""
        self.nickname = ""
        self.avatar = ""
        self.level = 0
        self.cookies = []
        self.is_logged_in = False

        # 多账号支持
        self.accounts = {}  # {slot_name: {heybox_id, nickname, avatar, level, cookies}}
        # 小号登录待确认的临时 Session（由 app.py 设置和清理）
        self._pending_alt_session = None

        # 保存锁 + Cookie 更新防抖
        self._save_lock = threading.Lock()
        self._last_cookie_save = 0.0

        # 注册 Cookie 更新回调
        heybox_api.set_cookie_update_handler(self._on_cookies_updated)

    def _on_cookies_updated(self, cookies):
        """API 响应中 Cookie 更新时触发（2 秒防抖，避免高频写盘）。"""
        self.cookies = heybox_api.get_cookies_list()
        with self._save_lock:
            now = time.time()
            if now - self._last_cookie_save < 2:
                return
            self._last_cookie_save = now
        self.save()

    def save(self):
        """持久化会话数据（含多账号，原子写入）。"""
        data = {
            "heybox_id": self.heybox_id,
            "nickname": self.nickname,
            "avatar": self.avatar,
            "level": self.level,
            "cookies": self.cookies,
            "accounts": self.accounts,
        }
        with self._save_lock:
            try:
                os.makedirs(self._data_dir, exist_ok=True)
                atomic_write_json(self._session_path, data)
            except Exception as e:
                self._logger.error(f"保存会话失败: {e}")

    def load(self) -> bool:
        """从文件加载会话数据（主文件损坏时回退 .bak 备份）。返回是否加载成功。"""
        try:
            if not os.path.exists(self._session_path):
                return False
            data = load_json_with_backup(self._session_path, None)
            if not isinstance(data, dict):
                self._logger.error("加载会话失败: 会话文件与备份均不可用")
                return False

            self.heybox_id = data.get("heybox_id", "")
            self.nickname = data.get("nickname", "")
            self.avatar = data.get("avatar", "")
            self.level = data.get("level", 0)
            self.cookies = data.get("cookies", [])

            if self.cookies:
                heybox_api.set_cookies(self.cookies)

            # 加载副账号
            if data.get("accounts"):
                self.accounts = data["accounts"]
                for slot, acc in self.accounts.items():
                    if acc.get("cookies") and acc.get("heybox_id"):
                        heybox_api.register_account_session(slot, acc["cookies"])

            return bool(self.heybox_id)
        except Exception as e:
            self._logger.error(f"加载会话失败: {e}")
            return False

    def check_login_status(self) -> bool:
        """
        检测当前登录状态是否有效（含副号验证）。
        返回: True 表示有效, False 表示失效。
        """
        if not self.heybox_id:
            self._logger.warn("登录状态检测: 未找到已保存的会话")
            self.is_logged_in = False
            return False

        try:
            result = heybox_api.get_user_permission(self.heybox_id)
            if result.get("valid"):
                self.is_logged_in = True
                # 更新 Cookie
                self.cookies = heybox_api.get_cookies_list()
                self.save()
                self._logger.info(f"登录状态检测: 主号「{self.nickname}」(ID:{self.heybox_id}) 有效")
            else:
                self._logger.warn("登录状态检测: 主号会话已失效，需要重新登录")
                self.is_logged_in = False
                self.heybox_id = ""
                self.save()
                return False
        except Exception as e:
            # 网络异常不清除登录状态，本次跳过校验
            self._logger.warn(f"主号登录状态检测网络异常: {e}，本次跳过校验")
            return False

        # 验证副号登录状态
        self._check_alt_accounts()
        return True

    def _check_alt_accounts(self):
        """验证所有副账号的登录状态，失效的自动移除。"""
        invalid_slots = []
        for slot, acc in list(self.accounts.items()):
            if not acc.get("cookies") or not acc.get("heybox_id"):
                continue
            alt_session = heybox_api.get_account_session(slot)
            if not alt_session:
                continue
            try:
                # 使用副号独立 Session 验证有效性
                result = heybox_api.get_user_permission(acc["heybox_id"], session=alt_session)
                if result.get("valid"):
                    self._logger.info(f"登录状态检测: 副号「{acc.get('nickname', slot)}」(ID:{acc['heybox_id']}) 有效")
                elif result.get("valid") is False:
                    self._logger.warn(f"副号「{acc.get('nickname', slot)}」登录已失效，移除")
                    invalid_slots.append(slot)
            except Exception as e:
                # 网络异常等只告警不移除，避免误删可用副号
                self._logger.warn(f"副号「{acc.get('nickname', slot)}」状态检测异常: {e}，本次跳过")

        for slot in invalid_slots:
            heybox_api.remove_account_session(slot)
            self.accounts.pop(slot, None)

        if invalid_slots:
            self.save()
            self._logger.info(f"已清除 {len(invalid_slots)} 个失效副号")

    def swap_primary(self, slot: str) -> dict:
        """将指定副号提升为主号，原主号降为副号。

        原子交换内存字段 + 重建 Session 层 + 落盘；任何一步失败即回滚。
        调用方需保证机器人已停止。返回交换前后的账号信息。
        """
        acc = self.accounts.get(slot)
        if not acc or not acc.get("cookies") or not acc.get("heybox_id"):
            raise ValueError("副号不存在或登录信息不完整")
        if not self.heybox_id:
            raise ValueError("主号未登录，无法切换")
        if acc["heybox_id"] == self.heybox_id:
            raise ValueError("该副号与主号为同一账号，无需切换")

        old_primary = {
            "heybox_id": self.heybox_id,
            "nickname": self.nickname,
            "avatar": self.avatar,
            "level": self.level,
            "cookies": self.cookies,
        }
        new_primary = {
            "heybox_id": acc["heybox_id"],
            "nickname": acc.get("nickname", ""),
            "avatar": acc.get("avatar", ""),
            "level": acc.get("level", 0),
            "cookies": acc["cookies"],
        }
        old_acc_entry = dict(acc)
        new_slot = f"alt_{old_primary['heybox_id']}"
        demoted_entry = {
            "heybox_id": old_primary["heybox_id"],
            "nickname": old_primary["nickname"],
            "avatar": old_primary["avatar"],
            "level": old_primary["level"],
            "cookies": old_primary["cookies"],
        }

        try:
            # 1) 内存字段互换
            self.heybox_id = new_primary["heybox_id"]
            self.nickname = new_primary["nickname"]
            self.avatar = new_primary["avatar"]
            self.level = new_primary["level"]
            self.cookies = new_primary["cookies"]
            self.accounts.pop(slot, None)
            self.accounts[new_slot] = demoted_entry

            # 2) Session 层重建：主 Session 换用新主号 Cookie，旧主号注册为副号
            heybox_api.remove_account_session(slot)
            heybox_api.clear_main_cookies()
            heybox_api.set_cookies(new_primary["cookies"])
            heybox_api.register_account_session(new_slot, demoted_entry["cookies"])

            # 3) 落盘
            self.save()
        except Exception:
            # 回滚内存字段与 accounts
            self.heybox_id = old_primary["heybox_id"]
            self.nickname = old_primary["nickname"]
            self.avatar = old_primary["avatar"]
            self.level = old_primary["level"]
            self.cookies = old_primary["cookies"]
            self.accounts.pop(new_slot, None)
            self.accounts[slot] = old_acc_entry
            # 回滚 Session 层（尽力而为）
            try:
                heybox_api.remove_account_session(new_slot)
                heybox_api.clear_main_cookies()
                heybox_api.set_cookies(old_primary["cookies"])
                heybox_api.register_account_session(slot, old_acc_entry["cookies"])
            except Exception as rollback_err:
                self._logger.error(f"主副切换回滚 Session 层失败: {rollback_err}")
            try:
                self.save()
            except Exception:
                pass
            raise

        self._logger.info(
            f"主副切换完成: 新主号「{self.nickname}」(ID:{self.heybox_id})，"
            f"旧主号「{old_primary['nickname']}」(ID:{old_primary['heybox_id']}) 已转为副号"
        )
        return {"old_primary": old_primary, "new_primary": new_primary, "new_slot": new_slot}

    def logout(self):
        """退出登录：仅清除主号凭证。副号 Cookie 相互独立，退出主号时保留。"""
        self.heybox_id = ""
        self.nickname = ""
        self.avatar = ""
        self.level = 0
        self.cookies = []
        self.is_logged_in = False

        # 清除主会话 Cookie（副号使用各自独立的 Session，不受影响）
        heybox_api.clear_main_cookies()

        # 重新保存会话文件：主号字段清空、副号数据保留
        # （不能直接删除文件，否则副号会随文件一起丢失）
        self.save()

        alt_count = len(self.accounts)
        if alt_count:
            self._logger.info(f"已退出登录（已保留 {alt_count} 个副号）")
        else:
            self._logger.info("已退出登录")

    def get_info(self) -> dict:
        """获取当前登录信息。"""
        return {
            "heybox_id": self.heybox_id,
            "nickname": self.nickname,
            "avatar": self.avatar,
            "level": self.level,
            "is_logged_in": self.is_logged_in,
        }


# 全局实例
_session_instance = None


def get_session() -> SessionManager:
    global _session_instance
    if _session_instance is None:
        _session_instance = SessionManager()
    return _session_instance