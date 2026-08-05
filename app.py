"""
小黑盒AI自动回复 - Web 主应用
Flask 后端，提供 API 接口和页面渲染
"""

import json
import os
import queue
import re
import secrets
import sys
import time
import threading

from flask import Flask, render_template, request, jsonify, Response, send_file, make_response
from flask_cors import CORS

# 脚本直跑时需要将项目根目录加入 Python 路径，否则找不到 config/core 等包
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.config_manager import get_config, DEFAULT_PROMPT
from logger.log_manager import get_logger
from core.session_manager import get_session
from core.llm_client import get_llm_client
from core.access_control import get_access_control
from core.bot_engine import get_bot_engine
from core.stats import get_stats
from core import heybox_api

_server_start_time = time.time()

# 服务监听地址常量
HOST = "127.0.0.1"
PORT = 5500

app = Flask(__name__,
            template_folder=os.path.join(os.path.dirname(__file__), "web", "templates"),
            static_folder=os.path.join(os.path.dirname(__file__), "web", "static"))
CORS(app, origins=[f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"])

# 初始化各模块
config = get_config()
logger = get_logger()
session_manager = get_session()
llm_client = get_llm_client()
access_control = get_access_control()
bot_engine = get_bot_engine()

# API 访问令牌：每次启动随机生成，通过模板变量与 HttpOnly Cookie 下发给前端
API_TOKEN = secrets.token_hex(16)

# 副号登录流程锁（二维码获取与状态查询串行化，防止并发竞态）
_alt_login_lock = threading.Lock()
# Steam 榜单爬取锁（防止重复触发）
_scrape_lock = threading.Lock()


def _require_token():
    """校验 /api/ 请求的访问令牌（请求头 X-Api-Token 或 Cookie api_token）。"""
    if not request.path.startswith("/api/"):
        return None
    token = request.headers.get("X-Api-Token") or request.cookies.get("api_token")
    if token != API_TOKEN:
        return jsonify({"ok": False, "error": "未授权"}), 401
    return None


@app.before_request
def _check_api_token():
    """所有 /api/ 开头的请求统一鉴权。"""
    return _require_token()


@app.route("/")
def index():
    """主页面。注入 API Token 并写入 HttpOnly Cookie。"""
    resp = make_response(render_template("index.html", api_token=API_TOKEN))
    resp.set_cookie("api_token", API_TOKEN, path="/", samesite="Strict", httponly=True)
    return resp


# ==============================
# 登录相关 API
# ==============================


@app.route("/api/login/status", methods=["GET"])
def api_login_status():
    """获取登录状态。"""
    info = session_manager.get_info()
    return jsonify(info)


@app.route("/api/login/qrcode", methods=["GET"])
def api_get_qrcode():
    """获取登录二维码。"""
    try:
        result = heybox_api.get_qrcode()
        logger.info(f"获取登录二维码成功: qr_id={result.get('qr_id', '')[:8]}...")
        return jsonify({"ok": True, "data": result})
    except Exception as e:
        logger.error(f"获取登录二维码失败: {e}")
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/login/qrstate/<qr_id>", methods=["GET"])
def api_qr_state(qr_id):
    """查询二维码状态。"""
    try:
        state = heybox_api.get_qrcode_state(qr_id)
        error = state.get("error", "")

        if error == "ok":
            # 登录成功，保存会话
            session_manager.heybox_id = state.get("heybox_id", "")
            session_manager.nickname = state.get("nickname", "")
            session_manager.avatar = state.get("avatar", "")
            account_detail = state.get("account_detail", {})
            level_info = account_detail.get("level_info", {})
            session_manager.level = level_info.get("level", 0)
            session_manager.cookies = heybox_api.get_cookies_list()
            session_manager.is_logged_in = True
            session_manager.save()
            logger.info(f"登录成功: {session_manager.nickname} (ID: {session_manager.heybox_id})")
            return jsonify({
                "ok": True,
                "state": "ok",
                "nickname": session_manager.nickname,
                "avatar": session_manager.avatar,
            })

        return jsonify({"ok": True, "state": error})
    except Exception as e:
        logger.error(f"查询二维码状态失败: {e}")
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/login/logout", methods=["POST"])
def api_logout():
    """退出登录。"""
    try:
        bot_engine.stop()
        # 清理可能残留的副号登录临时 Session
        pending = getattr(session_manager, "_pending_alt_session", None)
        if pending is not None:
            try:
                pending.close()
            except Exception:
                pass
            try:
                del session_manager._pending_alt_session
            except AttributeError:
                pass
        session_manager.logout()
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"退出登录失败: {e}")
        return jsonify({"ok": False, "error": str(e)})


# ==============================
# 多账号登录 API
# ==============================


@app.route("/api/login/alt/qrcode", methods=["GET"])
def api_get_alt_qrcode():
    """获取副号登录二维码（使用独立 Session）。全程持锁串行化，防止并发竞态。"""
    with _alt_login_lock:
        try:
            # 清理上一次可能残留的待定 session，防止对象泄漏
            if hasattr(session_manager, "_pending_alt_session") and session_manager._pending_alt_session is not None:
                try:
                    session_manager._pending_alt_session.close()
                except Exception:
                    pass
                try:
                    del session_manager._pending_alt_session
                except AttributeError:
                    pass
            alt_session = heybox_api._make_session()
            result = heybox_api.get_qrcode(session=alt_session)
            # 暂存 alt_session 到 session_manager
            session_manager._pending_alt_session = alt_session
            logger.info(f"获取副号登录二维码成功: qr_id={result.get('qr_id', '')[:8]}...")
            return jsonify({"ok": True, "data": result})
        except Exception as e:
            logger.error(f"获取副号登录二维码失败: {e}")
            return jsonify({"ok": False, "error": str(e)})

@app.route("/api/login/alt/qrstate/<qr_id>", methods=["GET"])
def api_alt_qr_state(qr_id):
    """查询副号二维码状态并保存到 accounts（使用独立 Session）。全程持锁串行化。"""
    with _alt_login_lock:
        _alt_session = getattr(session_manager, "_pending_alt_session", None)
        if not _alt_session:
            return jsonify({"ok": False, "error": "副号登录 Session 已失效，请重新获取二维码"})
        try:
            state = heybox_api.get_qrcode_state(qr_id, session=_alt_session)
            error = state.get("error", "")
            if error == "ok":
                heybox_id = state.get("heybox_id", "")
                nickname = state.get("nickname", "")
                avatar = state.get("avatar", "")
                cookies = heybox_api.get_cookies_list(session=_alt_session)
                account_detail = state.get("account_detail", {})
                level_info = account_detail.get("level_info", {})
                level = level_info.get("level", 0)
                # 使用 heybox_id 作为 slot 键，支持多个副号
                slot = f"alt_{heybox_id}"
                session_manager.accounts[slot] = {
                    "heybox_id": heybox_id,
                    "nickname": nickname,
                    "avatar": avatar,
                    "level": level,
                    "cookies": cookies,
                }
                # 注册独立 Session
                heybox_api.register_account_session(slot, cookies)
                session_manager.save()
                # 登录成功后清理临时 Session 引用（先 close 再 del）
                if hasattr(session_manager, "_pending_alt_session"):
                    try:
                        session_manager._pending_alt_session.close()
                    except Exception:
                        pass
                    try:
                        del session_manager._pending_alt_session
                    except AttributeError:
                        pass
                logger.info(f"副号登录成功: {nickname} (ID: {heybox_id}, slot: {slot})")
                return jsonify({
                    "ok": True, "state": "ok",
                    "nickname": nickname, "avatar": avatar, "slot": slot,
                })
            return jsonify({"ok": True, "state": error})
        except Exception as e:
            # 二维码超时或异常时清理临时 Session
            if hasattr(session_manager, "_pending_alt_session"):
                try:
                    session_manager._pending_alt_session.close()
                except Exception:
                    pass
                try:
                    del session_manager._pending_alt_session
                except AttributeError:
                    pass
            logger.error(f"查询副号二维码状态失败: {e}")
            return jsonify({"ok": False, "error": str(e)})

@app.route("/api/login/alt/accounts", methods=["GET"])
def api_get_alt_accounts():
    """获取已登录的副号列表。"""
    accounts = []
    for slot, acc in (session_manager.accounts or {}).items():
        if acc.get("heybox_id"):
            accounts.append({
                "slot": slot, "heybox_id": acc["heybox_id"],
                "nickname": acc.get("nickname", ""),
                "avatar": acc.get("avatar", ""),
                "enabled": acc.get("enabled", True),
                "disabled_at": acc.get("disabled_at", 0),
            })
    return jsonify({"ok": True, "data": accounts})

@app.route("/api/login/alt/accounts/<slot>/toggle", methods=["POST"])
def api_toggle_alt_account(slot):
    """切换副号的启用/禁用状态。"""
    acc = session_manager.accounts.get(slot, {})
    if not acc:
        return jsonify({"ok": False, "error": "副号不存在"})
    data = request.get_json() or {}
    enabled = data.get("enabled", True)
    if not enabled:
        bot_config = config.get_bot_config()
        if bot_config.get("standby_mode") and slot == bot_config.get("standby_slot", ""):
            return jsonify({"ok": False, "error": "该副号正在作为替身使用，请先关闭替身模式"})
    acc["enabled"] = bool(enabled)
    if enabled:
        acc.pop("disabled_at", None)
    session_manager.save()
    logger.info(f"副号 {slot} 已{'启用' if enabled else '禁用'}")
    return jsonify({"ok": True, "slot": slot, "enabled": enabled})

@app.route("/api/login/alt/accounts/<slot>", methods=["DELETE"])
def api_remove_alt_account(slot):
    """移除一个副号。"""
    if bot_engine.is_running:
        return jsonify({"ok": False, "error": "机器人运行中，请先停止机器人再移除副号"})
    bot_config = config.get_bot_config()
    if bot_config.get("standby_mode") and slot == bot_config.get("standby_slot", ""):
        return jsonify({"ok": False, "error": "该副号正在作为替身使用，请先关闭替身模式"})
    heybox_api.remove_account_session(slot)
    session_manager.accounts.pop(slot, None)
    session_manager.save()
    logger.info(f"已移除副号: {slot}")
    return jsonify({"ok": True})

@app.route("/api/login/alt/accounts/<slot>/promote", methods=["POST"])
def api_promote_alt_account(slot):
    """将副号提升为主号，原主号自动转为副号。"""
    if bot_engine.is_running:
        return jsonify({"ok": False, "error": "机器人运行中，请先停止机器人再切换主副号"})
    acc = session_manager.accounts.get(slot)
    if not acc or not acc.get("cookies") or not acc.get("heybox_id"):
        return jsonify({"ok": False, "error": "副号不存在或登录信息不完整"})

    # 该副号正被设为替身：提升为主号后自动关闭替身模式
    # （合并现有配置提交，避免 set_bot_config 将间隔字段重置为默认值）
    standby_cleared = False
    bot_config = config.get_bot_config()
    if bot_config.get("standby_mode") and slot == bot_config.get("standby_slot", ""):
        merged = dict(bot_config)
        merged["standby_mode"] = False
        merged["standby_slot"] = ""
        config.set_bot_config(merged)
        standby_cleared = True
        logger.info(f"副号 {slot} 升为主号，替身模式已自动关闭")

    try:
        result = session_manager.swap_primary(slot)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)})
    except Exception as e:
        logger.error(f"主副切换失败: {e}")
        return jsonify({"ok": False, "error": f"切换失败: {e}"})

    # 交换后验证新主号登录态（副号刚通过有效性验证，失败仅告警）
    try:
        perm = heybox_api.get_user_permission(session_manager.heybox_id)
        if not perm.get("valid"):
            logger.warn("主副切换后新主号登录态验证未通过，下次操作可能需要重新登录")
    except Exception as e:
        logger.warn(f"主副切换后新主号验证异常: {e}，本次跳过校验")

    new_primary = result["new_primary"]
    return jsonify({
        "ok": True,
        "standby_cleared": standby_cleared,
        "data": {
            "heybox_id": new_primary["heybox_id"],
            "nickname": new_primary["nickname"],
            "avatar": new_primary["avatar"],
        },
    })


# ==============================
# 对话任务面板 API
# ==============================


@app.route("/api/tasks", methods=["GET"])
def api_tasks():
    """查询 @ 触发的回复任务（关键词命中提问/回复，支持 UID/日期/状态过滤）。"""
    from core.task_tracker import get_task_tracker
    tracker = get_task_tracker()
    tasks = tracker.list(
        keyword=request.args.get("keyword", ""),
        uid=request.args.get("uid", ""),
        date=request.args.get("date", ""),
        status=request.args.get("status", ""),
        limit=request.args.get("limit", 200),
    )
    return jsonify({"ok": True, "data": tasks, "status_labels": tracker.STATUS_LABELS})


# ==============================
# 配置相关 API
# ==============================


@app.route("/api/config", methods=["GET"])
def api_get_config():
    """获取当前配置。"""
    return jsonify({
        "ok": True,
        "data": config.get_all(),
        "providers": config.get_providers(),
    })


@app.route("/api/config/bot", methods=["POST"])
def api_set_bot_config():
    """设置机器人配置。body 中 dry_run=True 时仅校验不保存。"""
    try:
        data = request.get_json() or {}
        dry_run = bool(data.pop("dry_run", False))
        result = config.set_bot_config(data, dry_run=dry_run)

        # dry_run 是前端保存前的内部预校验，紧随其后会有正式保存，
        # 不落任何日志，避免"校验通过（未保存）"这类误导性内容刷屏
        if not dry_run:
            if result.get("warnings"):
                for w in result["warnings"]:
                    logger.warn(f"配置修正: {w}")

            if result.get("risk_warning"):
                logger.warn("用户设置了低于安全阈值的参数")

            logger.info(_describe_bot_config_change(data))
        return jsonify(result)
    except Exception as e:
        logger.error(f"设置机器人配置失败: {e}")
        return jsonify({"ok": False, "error": str(e)})


# 机器人配置字段中文标签（变更日志用）
_BOT_FIELD_LABELS = {
    "mode": "访问控制模式",
    "admin_id": "管理员 ID",
    "test_steam_id": "测试 Steam ID",
    "white_list": "白名单",
    "frequency": "每小时调用次数",
    "init_wait_time": "初始回复间隔",
    "max_wait_time": "最大回复间隔",
    "increment": "间隔递增",
    "max_messages_per_round": "每轮最大消息数",
    "parallel": "并行处理",
    "parallel_count": "并行数量",
    "multi_account": "多账号交叉回复",
    "standby_mode": "替身模式",
    "standby_slot": "替身账号",
    "auto_disable_alt_on_risk": "风控自动禁用副号",
}

_MODE_VALUE_LABELS = {"white_list": "白名单模式", "frequency": "频率限制模式"}


def _describe_bot_config_change(data: dict) -> str:
    """按实际提交的字段生成机器人配置变更日志。"""
    if not data:
        return "机器人配置已保存（无字段变更）"
    # 单字段开关/选择的静默保存：直接描述用户操作
    if len(data) == 1:
        key, value = next(iter(data.items()))
        label = _BOT_FIELD_LABELS.get(key)
        if label and isinstance(value, bool):
            return f"{label}已{'开启' if value else '关闭'}"
        if key == "standby_slot":
            return f"替身账号已设置为 {value}" if value else "替身账号已清空"
    # 完整保存：逐字段详细输出
    parts = []
    for key, value in data.items():
        label = _BOT_FIELD_LABELS.get(key, key)
        if isinstance(value, bool):
            text = "开启" if value else "关闭"
        elif key == "mode":
            text = _MODE_VALUE_LABELS.get(value, str(value))
        elif key == "white_list":
            text = f"{len(value)} 人" if isinstance(value, list) else str(value)
        elif key in ("init_wait_time", "max_wait_time", "increment"):
            text = f"{value}s"
        elif value == "":
            text = "（空）"
        else:
            text = str(value)
        parts.append(f"{label}={text}")
    return f"机器人配置已保存: {', '.join(parts)}"


@app.route("/api/config/llm", methods=["POST"])
def api_set_llm_config():
    """设置 LLM 配置。"""
    try:
        data = request.get_json() or {}
        config.set_llm_config(data)
        logger.info(_describe_llm_config_change(data))
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"设置 LLM 配置失败: {e}")
        return jsonify({"ok": False, "error": str(e)})


# LLM 配置字段中文标签（变更日志用）
_LLM_FIELD_LABELS = {
    "vendor": "提供商",
    "api_key": "API Key",
    "base_url": "Base URL",
    "api_path": "API 路径",
    "model": "模型",
    "max_tokens": "最大 Token 数",
    "web_search": "联网搜索",
    "show_reasoning": "流式思维链展示",
}


def _describe_llm_config_change(data: dict) -> str:
    """按实际提交的字段生成 LLM 配置变更日志（API Key 永不落日志）。"""
    if not data:
        return "LLM 配置已更新（无字段变更）"
    # 单字段开关的静默保存：直接描述用户操作，不再输出空 vendor/model
    if set(data) == {"show_reasoning"}:
        return f"流式思维链展示已{'开启' if data['show_reasoning'] else '关闭'}"
    if set(data) == {"web_search"}:
        return f"联网搜索已{'开启' if data['web_search'] else '关闭'}（主回复模型）"
    # 完整保存：逐字段详细输出
    parts = []
    for key, value in data.items():
        label = _LLM_FIELD_LABELS.get(key, key)
        if key == "api_key":
            text = "已更新（脱敏）" if value else "已清空"
        elif isinstance(value, bool):
            text = "开启" if value else "关闭"
        else:
            text = str(value)
        parts.append(f"{label}={text}")
    return f"LLM 配置已更新: {', '.join(parts)}"


@app.route("/api/config/llm_baidu_search", methods=["POST"])
def api_set_llm_baidu_search_config():
    """设置百度搜索 API 配置。"""
    try:
        data = request.get_json() or {}
        config.set_llm_baidu_search_config(data)
        logger.info(f"百度搜索配置已更新: model={data.get('model', '')}")
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"设置百度搜索配置失败: {e}")
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/config/llm_search_judge", methods=["POST"])
def api_set_llm_search_judge_config():
    """设置联网搜索需求判断模型配置。"""
    try:
        data = request.get_json() or {}
        config.set_llm_search_judge_config(data)
        logger.info(f"搜索判断模型配置已更新: enabled={data.get('enabled')}, model={data.get('model', '')}")
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"设置搜索判断模型配置失败: {e}")
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/config/steam", methods=["POST"])
def api_set_steam_config():
    """设置 Steam 库存评价配置。"""
    try:
        data = request.get_json() or {}
        # 使用 ConfigManager 的公开 setter（含类型验证与钳制）
        config.set_steam_config(data)
        logger.info("Steam 库存评价配置已更新")
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"设置 Steam 配置失败: {e}")
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/steam/scrape/status", methods=["GET"])
def api_steam_scrape_status():
    """获取 Steam 游戏榜单各分类的时效状态。"""
    try:
        from core.steam_games import get_scrape_status
        status = get_scrape_status()
        return jsonify({"ok": True, "data": status})
    except Exception as e:
        logger.error(f"获取 Steam 榜单状态失败: {e}")
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/steam/scrape/trigger", methods=["POST"])
def api_steam_scrape_trigger():
    """手动触发 Steam 游戏榜单爬取，后台执行。"""
    try:
        from core.steam_games import update_local_db

        def _run_scrape():
            # 非阻塞取锁，已有爬取在进行中时直接跳过（update_local_db 内部另有模块锁双保险）
            if not _scrape_lock.acquire(blocking=False):
                logger.warn("[Steam 爬取] 爬取已在进行中，本次触发忽略")
                return
            try:
                logger.info("[Steam 爬取] 手动触发开始")
                update_local_db(force=True)
                logger.info("[Steam 爬取] 手动触发完成")
            except Exception as e:
                logger.error(f"[Steam 爬取] 手动触发失败: {e}")
            finally:
                _scrape_lock.release()

        threading.Thread(target=_run_scrape, daemon=True).start()
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"触发 Steam 榜单爬取失败: {e}")
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/config/llm_steam", methods=["POST"])
def api_set_llm_steam_config():
    """设置 Steam 评价 LLM 配置。"""
    try:
        data = request.get_json() or {}
        config.set_llm_steam_config(data)
        logger.info(f"Steam 评价模型配置已更新: vendor={data.get('vendor', '')}, model={data.get('model', '')}")
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"设置 Steam 评价 LLM 配置失败: {e}")
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/config/llm_search", methods=["POST"])
def api_set_llm_search_config():
    """设置搜索关键词 LLM 配置。"""
    try:
        data = request.get_json() or {}
        config.set_llm_search_config(data)
        logger.info(f"搜索 LLM 配置已更新: vendor={data.get('vendor', '')}, model={data.get('model', '')}")
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"设置搜索 LLM 配置失败: {e}")
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/config/prompt", methods=["GET"])
def api_get_prompt():
    """获取提示词。"""
    return jsonify({
        "ok": True,
        "content": config.get_prompt(),
        "is_default": not config.has_custom_prompt(),
    })


@app.route("/api/config/prompt", methods=["POST"])
def api_set_prompt():
    """设置提示词。"""
    try:
        data = request.get_json() or {}
        content = data.get("content", "")
        config.set_prompt(content)
        logger.info("提示词已更新")
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"设置提示词失败: {e}")
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/config/reset", methods=["POST"])
def api_reset_config():
    """重置所有配置为默认值。"""
    try:
        config.reset_all()
        logger.info("所有配置已重置为默认值")
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"重置配置失败: {e}")
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/config/prompt/reset", methods=["POST"])
def api_reset_prompt():
    """恢复默认提示词。"""
    try:
        config.reset_prompt()
        logger.info("提示词已恢复为默认")
        return jsonify({"ok": True, "content": DEFAULT_PROMPT})
    except Exception as e:
        logger.error(f"恢复默认提示词失败: {e}")
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/config/steam_prompt", methods=["GET"])
def api_get_steam_prompt():
    """获取 Steam 评价提示词。"""
    content = config.get_steam_prompt()
    return jsonify({
        "ok": True,
        "content": content,
        "is_default": not config.has_custom_steam_prompt(),
    })


@app.route("/api/config/steam_prompt", methods=["POST"])
def api_set_steam_prompt():
    """设置 Steam 评价提示词。"""
    try:
        data = request.get_json() or {}
        content = data.get("content", "")
        config.set_steam_prompt(content)
        logger.info("Steam 评价提示词已更新")
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"设置 Steam 评价提示词失败: {e}")
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/config/steam_prompt/reset", methods=["POST"])
def api_reset_steam_prompt():
    """恢复默认 Steam 评价提示词。"""
    try:
        config.reset_steam_prompt()
        logger.info("Steam 评价提示词已恢复为默认")
        return jsonify({"ok": True, "content": config.get_steam_prompt()})
    except Exception as e:
        logger.error(f"恢复默认 Steam 评价提示词失败: {e}")
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/config/steam_recommend_prompt", methods=["GET"])
def api_get_steam_recommend_prompt():
    """获取 Steam 推荐提示词。"""
    content = config.get_steam_recommend_prompt()
    return jsonify({
        "ok": True,
        "content": content,
        "is_default": not config.has_custom_steam_recommend_prompt(),
    })


@app.route("/api/config/steam_recommend_prompt", methods=["POST"])
def api_set_steam_recommend_prompt():
    """设置 Steam 推荐提示词。"""
    try:
        data = request.get_json() or {}
        content = data.get("content", "")
        config.set_steam_recommend_prompt(content)
        logger.info("Steam 推荐提示词已更新")
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"设置 Steam 推荐提示词失败: {e}")
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/config/steam_recommend_prompt/reset", methods=["POST"])
def api_reset_steam_recommend_prompt():
    """恢复默认 Steam 推荐提示词。"""
    try:
        config.reset_steam_recommend_prompt()
        logger.info("Steam 推荐提示词已恢复为默认")
        return jsonify({"ok": True, "content": config.get_steam_recommend_prompt()})
    except Exception as e:
        logger.error(f"恢复默认 Steam 推荐提示词失败: {e}")
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/providers", methods=["GET"])
def api_get_providers():
    """获取 AI 提供商列表。"""
    return jsonify({"ok": True, "data": config.get_providers()})


# ==============================
# LLM 相关 API
# ==============================


@app.route("/api/llm/models", methods=["GET"])
def api_fetch_models():
    """获取模型列表。"""
    try:
        models = llm_client.fetch_models()
        return jsonify({"ok": True, "data": models})
    except Exception as e:
        logger.error(f"获取模型列表失败: {e}")
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/llm/test", methods=["POST"])
def api_test_llm():
    """测试 LLM 连通性。"""
    try:
        result = llm_client.test_connection()
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/llm/models_for", methods=["POST"])
def api_fetch_models_for():
    """使用请求体中的凭据获取模型列表（不污染当前配置）。"""
    try:
        data = request.get_json() or {}
        api_key = data.get("api_key", "")
        base_url = data.get("base_url", "")
        models = llm_client.fetch_models_for(api_key, base_url)
        return jsonify({"ok": True, "data": models})
    except Exception as e:
        logger.error(f"获取模型列表失败: {e}")
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/llm/test_for", methods=["POST"])
def api_test_llm_for():
    """使用请求体中的凭据测试模型连通性（不污染当前配置）。"""
    try:
        data = request.get_json() or {}
        result = llm_client.test_for(
            data.get("api_key", ""),
            data.get("base_url", ""),
            data.get("model", ""),
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/llm/test_search", methods=["POST"])
def api_test_search_llm():
    """测试搜索关键词模型连通性。"""
    try:
        result = llm_client.test_search_connection()
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/llm/test_judge", methods=["POST"])
def api_test_judge_llm():
    """测试判断模型连通性。"""
    try:
        result = llm_client.test_judge_connection()
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/search/test", methods=["POST"])
def api_test_search():
    """测试搜索 API（搜索"今日热点"）。"""
    try:
        from core.web_search import search_web
        result = search_web("今日热点")
        if result:
            logger.info(f"搜索 API 测试成功：搜索 今日热点")
            return jsonify({"ok": True, "response": result})
        else:
            logger.warn("搜索 API 测试未返回结果")
            return jsonify({"ok": False, "error": "搜索未返回结果，请检查 API Key 和网络连接"})
    except Exception as e:
        logger.error(f"搜索 API 测试失败: {e}")
        return jsonify({"ok": False, "error": str(e)})


# ==============================
# 机器人控制 API
# ==============================


@app.route("/api/bot/start", methods=["POST"])
def api_bot_start():
    """启动机器人。"""
    try:
        if not session_manager.is_logged_in:
            return jsonify({"ok": False, "error": "请先登录小黑盒账号"})

        # 直接启动，不做启动前会话自检（会话有效性由机器人运行时自行暴露）
        bot_engine.start()
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"启动机器人失败: {e}")
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/bot/stop", methods=["POST"])
def api_bot_stop():
    """停止机器人。"""
    try:
        bot_engine.stop()
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(f"停止机器人失败: {e}")
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/stats", methods=["GET"])
def api_get_stats():
    """获取运行统计数据。"""
    stats = get_stats().get_stats()
    # 补充当前模型名称
    llm = config.get_llm_config()
    llm_search = config.get_llm_search_config()
    llm_baidu = config.get_llm_baidu_search_config()
    stats["models"] = {
        "回复模型": llm.get("model", "未配置"),
        "搜索模型": llm_search.get("model", "未配置"),
        "判断模型": config.get_llm_search_judge_config().get("model", "未配置"),
        "联网搜索": llm_baidu.get("model", "未配置"),
        "AI评价库存": config.get_llm_steam_config().get("model", "未配置"),
    }
    stats["uptime_seconds"] = int(time.time() - _server_start_time)
    stats["steam_enabled"] = config.get_steam_config().get("enabled", False)
    return jsonify({"ok": True, "data": stats})


@app.route("/api/bot/status", methods=["GET"])
def api_bot_status():
    """获取机器人运行状态。"""
    status = bot_engine.get_status()
    status["is_logged_in"] = session_manager.is_logged_in
    return jsonify({"ok": True, "data": status})


# ==============================
# 日志相关 API
# ==============================


@app.route("/api/log/stream")
def api_log_stream():
    """
    SSE 日志流。
    实时向前端推送日志条目。
    """
    def generate():
        # maxsize 防止慢客户端积压；满时 logger 侧会移除该 listener
        log_queue = queue.Queue(maxsize=1000)
        logger.register_listener(log_queue)

        try:
            # 连接时先推送最近历史日志（如启动自检），避免新连接错过关键日志
            # get_recent_logs 返回倒序，reversed 后按时间正序推送，配合前端顶部插入逻辑
            for entry in reversed(logger.get_recent_logs(100)):
                yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"

            while True:
                try:
                    entry = log_queue.get(timeout=30)
                    yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    yield ": heartbeat\n\n"
        except GeneratorExit:
            pass
        except Exception:
            # 捕获其他异常（如 TypeError、MemoryError），
            # 避免 SSE 流以 500 错误终止导致前端 EventSource 无法区分正常关闭
            logger.warn("SSE 日志流因异常断开")
        finally:
            logger.unregister_listener(log_queue)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/log/clear", methods=["POST"])
def api_clear_logs():
    """清空日志。"""
    try:
        logger.clear_logs()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/log/export", methods=["GET"])
def api_export_logs():
    """导出日志文件。"""
    try:
        content = logger.export_logs()
        from io import BytesIO
        from datetime import datetime

        buffer = BytesIO()
        buffer.write(content.encode("utf-8"))
        buffer.seek(0)

        filename = f"bot_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        return send_file(
            buffer,
            mimetype="text/plain",
            as_attachment=True,
            download_name=filename,
        )
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/log/recent", methods=["GET"])
def api_recent_logs():
    """获取最近日志。"""
    count = request.args.get("count", 500, type=int)
    count = max(1, min(count, 2000))
    logs = logger.get_recent_logs(count)
    return jsonify({"ok": True, "data": logs})


# ==============================
# 访问控制相关 API
# ==============================


@app.route("/api/whitelist", methods=["GET"])
def api_get_whitelist():
    """获取白名单。"""
    bot_config = config.get_bot_config()
    return jsonify({
        "ok": True,
        "data": {
            "mode": bot_config.get("mode", "white_list"),
            "white_list": bot_config.get("white_list", []),
            "frequency": bot_config.get("frequency", 3),
        }
    })


@app.route("/api/whitelist/usage", methods=["GET"])
def api_get_usage():
    """获取调用次数统计。"""
    stats = access_control.get_usage_stats()
    return jsonify({"ok": True, "data": stats})


# ==============================
# 启动
# ==============================

def start_server():
    """启动 Web 服务器。使用 Flask 内置服务器（支持 SSE 流式响应）。"""
    import webbrowser

    # 抑制 Flask / Werkzeug 开发服务器警告
    import logging as _logging
    _logging.getLogger('werkzeug').setLevel(_logging.ERROR)

    url = f"http://{HOST}:{PORT}"

    # 延迟自动打开浏览器
    def open_browser():
        time.sleep(1.0)
        webbrowser.open(url)

    threading.Thread(target=open_browser, daemon=True).start()

    # 显示终端艺术字（橙色）
    art_path = os.path.join(os.path.dirname(__file__), "data", "art.txt")
    if os.path.exists(art_path):
        with open(art_path, "r", encoding="utf-8") as f:
            art = f.read()
        art = re.sub(r"\033\[[0-9;]*m", "", art)  # 剥离 art.txt 内嵌的旧颜色码
        if os.name == "nt":
            os.system("")  # 启用 Windows 终端 ANSI 转义序列支持
        print(f"\033[38;5;208m{art}\033[0m")

    logger.info(f"正在启动 Web 服务器: {url}")
    print(f"\n{'='*50}")
    print(f"  小黑盒AI自动回复")
    print(f"  Web 控制台: {url}")
    print(f"  按 Ctrl+C 停止服务")
    print(f"{'='*50}\n")

    try:
        app.run(host=HOST, port=PORT, debug=False, threaded=True)
    except KeyboardInterrupt:
        logger.info("收到停止信号，正在关闭服务...")
        bot_engine.stop()
        logger.info("服务已停止")
    except OSError as e:
        logger.error(f"服务器启动失败（端口可能被占用）: {e}")
        print(f"\n[错误] 无法启动服务器: {e}")
        print(f"请检查端口 {PORT} 是否被其他程序占用，或修改 app.py 中的 PORT 常量。")
        sys.exit(1)
    except Exception as e:
        logger.error(f"服务器异常退出: {e}")
        print(f"\n[错误] 服务器异常: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


def ensure_dependencies():
    """检查并自动安装缺失的依赖包（版本与 requirements.txt 对齐）。"""
    import subprocess as _subprocess
    required = {
        "tavily": "tavily-python==0.5.0",
        "PIL": "Pillow==11.0.0",
        "flask": "flask==3.1.0",
        "flask_cors": "flask-cors==5.0.1",
        "requests": "requests==2.32.3",
    }
    missing = []
    for imp_name, pkg_name in required.items():
        try:
            __import__(imp_name)
        except ImportError:
            missing.append(pkg_name)

    if missing:
        print(f"\n[安装] 检测到缺失依赖: {', '.join(missing)}")
        for pkg in missing:
            print(f"  正在安装 {pkg}...")
            try:
                # 优先使用清华镜像加速
                _subprocess.run([sys.executable, "-m", "pip", "install", pkg,
                                 "-i", "https://pypi.tuna.tsinghua.edu.cn/simple",
                                 "--default-timeout=120"], check=True, capture_output=True)
                print(f"  ✓ {pkg} 安装完成")
            except _subprocess.CalledProcessError:
                try:
                    # 镜像失败回退默认源再试一次
                    _subprocess.run([sys.executable, "-m", "pip", "install", pkg,
                                     "--default-timeout=120"], check=True, capture_output=True)
                    print(f"  ✓ {pkg} 安装完成（默认源）")
                except _subprocess.CalledProcessError as e:
                    # 中文 Windows 下 pip 错误输出可能是 GBK 编码，直接 decode 会抛
                    # UnicodeDecodeError 逃出捕获导致进程退出，统一用 replace 容错
                    err_detail = e.stderr.decode("utf-8", errors="replace") if e.stderr else "未知错误"
                    print(f"  ✗ {pkg} 安装失败: {err_detail}")
                    print(f"    对应功能（联网搜索等）将不可用")
        print("[安装] 依赖检查完成\n")


def _run_self_check():
    """在 Web 服务器启动后执行自检，确保前端能实时看到自检日志。"""
    time.sleep(0.5)  # 等待服务器启动，前端有机会连接 SSE

    # 检测小黑盒平台 API 可用性（登录检测依赖平台连通）
    api_ok, api_detail = heybox_api.check_api_available()
    if api_ok:
        logger.info("[自检] 小黑盒平台 API 连接正常")
    else:
        logger.warn(f"[自检] 小黑盒平台 API 不可用: {api_detail}，登录与机器人功能暂不可用")

    # 加载会话并检测登录状态（平台不可用时跳过，避免误报会话失效）
    if session_manager.load():
        if api_ok:
            logger.info("[自检] 已加载本地会话，正在检测登录状态...")
            session_manager.check_login_status()
        else:
            logger.info("[自检] 平台不可用，跳过登录状态检测（按上次状态显示）")
    else:
        logger.info("[自检] 未找到本地会话，请扫码登录")

    # 检测 Steam 游戏榜单时效
    try:
        from core.steam_games import get_scrape_status
        get_scrape_status(log_output=True)
    except Exception as e:
        logger.error(f"[自检] Steam 榜单时效检测失败: {e}")

    # 根据配置决定是否启动自动爬取
    steam_cfg = config.get_steam_config()
    if steam_cfg.get("auto_scrape", True):
        logger.info("[自检] 自动爬取游戏榜单已开启，将在后台检查更新")
        from core.steam_games import update_local_db

        def _db_update_timer():
            while True:
                try:
                    update_local_db()
                except Exception as e:
                    logger.error(f"[自动爬取] 游戏榜单更新失败: {e}")
                time.sleep(21600)  # 6小时 = 21600秒

        threading.Thread(target=_db_update_timer, daemon=True).start()
    else:
        logger.info("[自检] 自动爬取游戏榜单已关闭，跳过")

    logger.info("[自检] Web 控制台已就绪")


if __name__ == "__main__":
    # 启动时自动安装缺失依赖
    ensure_dependencies()

    # 程序自检开始
    logger.info("[自检] 程序启动")

    # 确保机器人状态重置为停止
    try:
        bot_engine.stop()
        logger.info("[自检] 机器人运行状态已重置")
    except Exception as e:
        print(f"[自检] 机器人状态重置失败: {e}")

    # 在独立后台线程中执行后续自检，确保前端连接后能看到完整自检日志
    threading.Thread(target=_run_self_check, daemon=True).start()

    start_server()