"""
小黑盒 API 封装
参考 heybox-bot-main/src/heybox/api/ 实现
"""

import json
import re
import threading
import time
from urllib.parse import urlencode, urlparse, parse_qs

import requests
from requests.cookies import RequestsCookieJar

from .heybox_sign import make_heybox_sign

API_BASE_URL = "https://api.xiaoheihe.cn"
# 评论写接口已迁移到独立域名（读接口仍在主域名）
COMMENT_API_BASE_URL = "https://workshopapi.xiaoheihe.cn"
MESSAGE_NUM_LIMIT = 20
POST_TREE_LIMIT = 20


class ReloginError(Exception):
    """登录态失效（需重新登录）时抛出，供上层做副号摘除/主号回退。"""
    pass

def _make_session():
    """创建带标准 headers 的独立 Session。"""
    s = requests.Session()
    s.headers.update({
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Origin": "https://www.xiaoheihe.cn",
        "Referer": "https://www.xiaoheihe.cn/",
        "Sec-Ch-Ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    })
    return s


# 主会话（向后兼容）
_session = _make_session()
# 主会话 Cookie jar 写操作锁
_session_lock = threading.Lock()

# 多账号独立 Session
_account_sessions = {}
_account_sessions_lock = threading.Lock()

_cookie_update_handler = None
_device_id = ""


def set_cookies(cookies: list, session=None):
    """将已有 Cookie 写入 HTTP 会话。默认写入主 _session，可指定目标 session。"""
    target = session or _session
    with _session_lock:
        for cookie in cookies:
            if isinstance(cookie, dict):
                target.cookies.set(
                    cookie.get("name", ""),
                    cookie.get("value", ""),
                    domain=cookie.get("domain", ""),
                    path=cookie.get("path", "/"),
                )
            elif hasattr(cookie, "name"):
                target.cookies.set_cookie(cookie)


def clear_main_cookies():
    """清空主会话的所有 Cookie。"""
    with _session_lock:
        _session.cookies.clear()


def get_cookies_list(session=None) -> list:
    """获取指定会话中所有 Cookie 的字典列表。"""
    target = session or _session
    result = []
    for cookie in target.cookies:
        result.append({
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain,
            "path": cookie.path,
            "secure": cookie.secure,
            "expires": cookie.expires,
        })
    return result


def register_account_session(slot_name: str, cookies: list) -> requests.Session:
    """注册一个副账号的独立 Session，写入 cookies 并返回该 Session。"""
    s = _make_session()
    set_cookies(cookies, session=s)
    with _account_sessions_lock:
        _account_sessions[slot_name] = s
    return s


def get_account_session(slot_name: str):
    """获取指定副账号的独立 Session。未注册时返回 None。"""
    with _account_sessions_lock:
        return _account_sessions.get(slot_name)


def remove_account_session(slot_name: str):
    """移除副账号 Session。"""
    with _account_sessions_lock:
        _account_sessions.pop(slot_name, None)


def set_cookie_update_handler(handler):
    """设置响应 Cookie 更新时的回调函数。"""
    global _cookie_update_handler
    _cookie_update_handler = handler


def set_device_id(device_id: str):
    """设置请求使用的设备 ID。"""
    global _device_id
    _device_id = device_id


def _request(method: str, api_path: str, heybox_id: str, extra_params: dict = None, form_data: dict = None, session=None, base_url: str = None) -> dict:
    """发起带签名的小黑盒 API 请求。可指定独立 session（多账号支持）。

    base_url 默认主域名 API_BASE_URL；评论写接口传 COMMENT_API_BASE_URL。
    签名仅针对 api_path，与域名无关，无需额外处理。
    """
    hkey, ts, nonce = make_heybox_sign(api_path)
    use_session = session or _session

    params = {
        "os_type": "web",
        "app": "web",
        "client_type": "web",
        "version": "999.0.4",
        "web_version": "2.5",
        "x_client_type": "web",
        "x_app": "heybox_website",
        "heybox_id": heybox_id,
        "x_os_type": "Windows",
        "device_info": "Chrome",
        "device_id": _device_id,
        "hkey": hkey,
        "_time": str(ts),
        "nonce": nonce,
    }

    if extra_params:
        params.update(extra_params)

    url = (base_url or API_BASE_URL) + api_path + "?" + urlencode(params)

    if method.upper() == "GET":
        # GET 幂等，网络异常时重试 1 次；POST 不重试（防重复发帖）
        for attempt in range(2):
            try:
                resp = use_session.get(url, timeout=15)
                break
            except (requests.ConnectionError, requests.Timeout):
                if attempt == 0:
                    time.sleep(1)
                    continue
                raise
    else:
        resp = use_session.post(
            url,
            data=form_data or {},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )

    if resp.status_code != 200:
        raise Exception(f"小黑盒 API 请求失败: {resp.status_code}")

    # 通知 Cookie 更新（仅主 session）
    if _cookie_update_handler and resp.cookies and (session is None or session is _session):
        _cookie_update_handler(list(resp.cookies))

    try:
        result = resp.json()
    except json.JSONDecodeError:
        result = {"status": "", "msg": "", "result": {}, "raw": resp.text}

    result["_raw"] = resp.text
    return result


def get_request(api_path: str, heybox_id: str = "", extra_params: dict = None, session=None) -> dict:
    """发起 GET 请求。"""
    return _request("GET", api_path, heybox_id, extra_params=extra_params, session=session)


def post_request(api_path: str, heybox_id: str = "", form_data: dict = None, session=None) -> dict:
    """发起 POST 请求。"""
    return _request("POST", api_path, heybox_id, form_data=form_data, session=session)


# ==============================
# 登录相关 API
# ==============================


def get_qrcode(session=None) -> dict:
    """获取登录二维码。可指定独立 session（多账号支持）。返回 {qr_url, expire, qr_id}"""
    resp = get_request("/account/get_qrcode_url/", session=session)
    if resp.get("status") != "ok":
        raise Exception(f"获取二维码失败，状态: {resp.get('status')}, 消息: {resp.get('msg')}")

    result = resp.get("result", {})
    qr_url = result.get("qr_url", "")
    expire = result.get("expire", 0)

    # 从 URL 解析 qr_id
    qr_id = ""
    if qr_url:
        parsed = urlparse(qr_url)
        qr_id = parse_qs(parsed.query).get("qr", [""])[0]

    return {
        "qr_url": qr_url,
        "expire": int(expire),
        "qr_id": qr_id,
    }


def check_api_available() -> tuple:
    """检测小黑盒平台 API 是否可用。返回 (是否可用, 失败详情)。

    先用免登录轻量接口 get_qrcode 探测主域名（带完整签名，GET 已内置一次网络重试），
    再探测评论写接口所在的 workshopapi 独立域名（拿到任意 HTTP 响应即视为可达）。
    用于程序启动自检。
    """
    try:
        get_qrcode()
    except requests.ConnectionError:
        return False, "网络连接失败（请检查网络或代理）"
    except requests.Timeout:
        return False, "连接超时（请检查网络或代理）"
    except requests.RequestException as e:
        return False, f"网络错误: {e}"
    except Exception as e:
        return False, f"平台响应异常: {e}"

    try:
        _session.get(COMMENT_API_BASE_URL + "/bbs/app/comment/create", timeout=10)
    except requests.ConnectionError:
        return False, "评论接口连接失败（workshopapi 域名不可达）"
    except requests.Timeout:
        return False, "评论接口连接超时（workshopapi 域名）"
    except requests.RequestException as e:
        return False, f"评论接口网络错误: {e}"

    return True, ""


def get_qrcode_state(qr_id: str, session=None) -> dict:
    """查询二维码状态。可指定独立 session（多账号支持）。"""
    use_session = session or _session
    resp = get_request("/account/qr_state/", extra_params={"qr": qr_id}, session=use_session)
    if resp.get("status") != "ok":
        raise Exception(f"获取二维码状态失败，状态: {resp.get('status')}, 消息: {resp.get('msg')}")

    result = resp.get("result", {})
    return {
        "error": result.get("error", ""),
        "error_msg": result.get("error_msg", ""),
        "heybox_id": result.get("heyboxid", ""),
        "avatar": result.get("avatar", ""),
        "nickname": result.get("nickname", ""),
        "account_detail": result.get("account_detail", {}),
        "_cookies": get_cookies_list(use_session),
    }


def get_user_permission(heybox_id: str, session=None) -> dict:
    """验证登录会话是否有效。可指定独立 session（多账号支持）。"""
    resp = get_request("/bbs/app/api/user/permission", heybox_id, session=session)
    status = resp.get("status", "")
    if status in ("ok", ""):
        # status 为空字符串但有 visitor_enabled 字段也视为成功
        if status == "ok":
            return {"valid": True}
        raw = resp.get("_raw", resp.get("raw", "{}"))
        if isinstance(raw, str):
            try:
                body = json.loads(raw)
                if "visitor_enabled" in body:
                    return {"valid": True}
            except json.JSONDecodeError:
                pass
        return {"valid": False}
    elif status in ("login", "relogin"):
        return {"valid": False}
    else:
        raise Exception(f"用户权限校验失败，状态={status}，消息={resp.get('msg', '')}")


# ==============================
# 消息相关 API
# ==============================


def get_at_message(heybox_id: str, offset: int = 0) -> list:
    """获取 @我的消息列表。"""
    resp = get_request("/bbs/app/user/message", heybox_id, extra_params={
        "message_type": "16",
        "app": "heybox",
        "offset": str(offset),
        "limit": str(MESSAGE_NUM_LIMIT),
        "no_more": "false",
    })

    if resp.get("status") != "ok":
        raise Exception(f"获取消息失败，状态: {resp.get('status')}, 消息: {resp.get('msg')}")

    result = resp.get("result", {})
    messages = result.get("messages", [])

    # 解析消息，兼容两种结构
    parsed_messages = []
    for msg in messages:
        link = msg.get("link", {})
        parsed = {
            "message_id": msg.get("message_id", 0),
            "user": msg.get("user_a", {}),
            "timestamp": msg.get("timestamp", ""),
            "comment_id": msg.get("comment_a_id", 0),
            "root_comment_id": msg.get("root_comment_id", 0),
            "link_id": msg.get("linkid", 0),
            "has_video": msg.get("has_video", 0),
            "text": msg.get("comment_a_text", ""),
            "message_type": msg.get("message_type", 0),
        }

        # 判断是否为帖子类型
        if parsed["message_type"] == 16:  # MessageTypeAtPost
            parsed["is_post"] = True
            parsed["link_id"] = link.get("linkid", 0) if link else parsed["link_id"]
            parsed["has_video"] = link.get("has_video", 0) if link else parsed["has_video"]
            parsed["text"] = link.get("description", "") if link else parsed["text"]
        else:
            parsed["is_post"] = False

        parsed_messages.append(parsed)

    return parsed_messages


# ==============================
# 帖子相关 API
# ==============================


def get_post_tree(heybox_id: str, link_id: int, page: int = 1) -> dict:
    """获取帖子内容和评论树。"""
    is_first = "1" if page == 1 else "0"

    resp = get_request("/bbs/app/link/tree", heybox_id, extra_params={
        "link_id": str(link_id),
        "page": str(page),
        "is_first": is_first,
        "index": "1",
        "limit": str(POST_TREE_LIMIT),
        "owner_only": "0",
    })

    if resp.get("status") != "ok":
        raise Exception(f"获取帖子评论树失败，状态: {resp.get('status')}, 消息: {resp.get('msg')}")

    result = resp.get("result", {})

    # 解析帖子链接信息
    link = result.get("link", {})
    parsed_link = _parse_post_link(link)

    # 解析评论
    comments = result.get("comments", [])
    parsed_comments = []
    for group in comments:
        comment_list = group.get("comment", [])
        parsed_group = []
        for c in comment_list:
            parsed_c = _parse_comment(c)
            parsed_group.append(parsed_c)
        if parsed_group:
            parsed_comments.append(parsed_group)

    return {
        "total_page": result.get("total_page", 0),
        "has_more_floors": result.get("has_more_floors", 0),
        "link": parsed_link,
        "comments": parsed_comments,
    }


def get_sub_comments(heybox_id: str, root_comment_id: int, last_val: int) -> dict:
    """拉取指定根评论下的更多子评论。"""
    resp = get_request("/bbs/app/comment/sub/comments", heybox_id, extra_params={
        "lastval": str(last_val),
        "root_comment_id": str(root_comment_id),
    })

    if resp.get("status") != "ok":
        raise Exception(f"获取子评论失败，状态: {resp.get('status')}, 消息: {resp.get('msg')}")

    result = resp.get("result", {})
    comments = result.get("comments", [])
    parsed_comments = [_parse_comment(c) for c in comments]

    return {
        "has_more": result.get("has_more", False),
        "last_val": result.get("lastval", 0),
        "comments": parsed_comments,
    }


# ==============================
# 评论相关 API
# ==============================


def create_comment(heybox_id: str, link_id: int, root_id: int, reply_id: int, text: str, session=None) -> dict:
    """创建评论，返回服务端生成的 comment_id。可指定独立 session（多账号支持）。

    评论写接口已迁移至 workshopapi 域名（COMMENT_API_BASE_URL）。
    """
    resp = _request("POST", "/bbs/app/comment/create", heybox_id, form_data={
        "link_id": str(link_id),
        "reply_id": str(reply_id),
        "root_id": str(root_id),
        "text": text,
    }, session=session, base_url=COMMENT_API_BASE_URL)

    if resp.get("status") != "ok":
        status = resp.get("status", "")
        msg = resp.get("msg", "")
        # 登录态失效：抛 ReloginError 供上层识别并回退主号
        if status in ("login", "relogin") or "重新登录" in msg:
            raise ReloginError(f"登录态失效，状态: {status}, 消息: {msg}")
        raise Exception(f"创建评论失败，状态: {status}, 消息: {msg}")

    raw = resp.get("_raw", resp.get("raw", "{}"))
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = resp
    else:
        data = resp

    comment_id = data.get("commentid", 0)
    if not comment_id:
        # 有些响应 commentid 在顶层
        comment_id = data.get("result", {}).get("commentid", 0)

    return {"comment_id": int(comment_id) if comment_id else 0}


def comment_post(heybox_id: str, link_id: int, text: str, session=None) -> dict:
    """评论帖子。"""
    return create_comment(heybox_id, link_id, -1, -1, text, session=session)


def comment_root(heybox_id: str, link_id: int, root_id: int, text: str, session=None) -> dict:
    """回复根评论。"""
    return create_comment(heybox_id, link_id, root_id, root_id, text, session=session)


def comment_reply(heybox_id: str, link_id: int, root_id: int, reply_id: int, text: str, session=None) -> dict:
    """回复指定评论。"""
    return create_comment(heybox_id, link_id, root_id, reply_id, text, session=session)


# ==============================
# 点赞相关 API
# ==============================


def like_post(heybox_id: str, link_id: int, unlike: bool = False, session=None) -> dict:
    """帖子点赞/取消赞。可指定独立 session（多账号支持）。

    award_type=1 点赞，0 取消；服务端幂等，重复点赞返回 ok。
    """
    resp = _request("POST", "/bbs/app/profile/award/link", heybox_id, form_data={
        "link_id": str(link_id),
        "award_type": "0" if unlike else "1",
    }, session=session)

    if resp.get("status") != "ok":
        raise Exception(f"帖子点赞失败，状态: {resp.get('status')}, 消息: {resp.get('msg')}")
    return {"ok": True}


def like_comment(heybox_id: str, comment_id: int, unlike: bool = False, session=None) -> dict:
    """评论点赞/取消赞。可指定独立 session（多账号支持）。

    support_type=1 点赞，0 取消。
    """
    resp = _request("POST", "/bbs/app/comment/support", heybox_id, form_data={
        "comment_id": str(comment_id),
        "support_type": "0" if unlike else "1",
    }, session=session)

    if resp.get("status") != "ok":
        raise Exception(f"评论点赞失败，状态: {resp.get('status')}, 消息: {resp.get('msg')}")
    return {"ok": True}


# ==============================
# 辅助函数
# ==============================


def _parse_post_link(link: dict) -> dict:
    """解析帖子链接信息。"""
    topics = link.get("topics", [])
    topic_name = topics[0].get("name", "") if topics else ""

    content_tags_list = link.get("content_tags", [])
    content_tags = [t.get("text", "") for t in content_tags_list if t.get("text")]

    text_content = link.get("text", "")
    description = ""
    img_urls = []

    if text_content:
        try:
            contents = json.loads(text_content) if isinstance(text_content, str) else text_content
            for item in contents:
                item_type = item.get("type", "")
                if item_type == "text":
                    description = item.get("text", "")
                elif item_type == "img":
                    url = item.get("url", "")
                    if url:
                        img_urls.append(url)
        except (json.JSONDecodeError, TypeError):
            pass

    return {
        "link_id": link.get("linkid", 0),
        "title": link.get("title", ""),
        "description": description,
        "img_urls": img_urls,
        "topic_name": topic_name,
        "content_tags": content_tags,
    }


def _parse_comment(comment: dict) -> dict:
    """解析评论信息。"""
    imgs = comment.get("imgs", [])
    img_urls = [img.get("url", "") for img in imgs if img.get("url")]

    return {
        "comment_id": comment.get("commentid", 0),
        "text": comment.get("text", ""),
        "img_urls": img_urls,
    }


def plain_heybox_mention_text(content: str) -> str:
    """
    将小黑盒 @ 链接文本转换为普通 @ 文本。
    """
    # 先还原转义字符
    replacements = {
        "\\u003c": "<", "\\u003C": "<",
        "\\u003e": ">", "\\u003E": ">",
        "\\u0026": "&", "\\u003d": "=",
        '\\"': '"',
    }
    for old, new in replacements.items():
        content = content.replace(old, new)

    # 匹配 <a ... data-user-id="..." ...>@用户名</a>
    import html
    pattern = re.compile(r'<a\b[^>]*\bdata-user-id\s*=\s*["\'][^"\']+["\'][^>]*>(.*?)</a>\s*', re.IGNORECASE | re.DOTALL)

    def _replace(m):
        mention_text = html.unescape(m.group(1).strip())
        return mention_text + " "

    return pattern.sub(_replace, content)