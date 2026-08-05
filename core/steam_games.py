"""
Steam 游戏库存评价模块 v2
通过 Steam Web API 获取用户游戏列表，结合本地数据库补充标签。
新增：玩家资料、成就、最近游玩、愿望单、多源推荐引擎。
"""

import json
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from logger.log_manager import get_logger
from .json_store import atomic_write_json

STEAM_BASE = 76561197960265728
STEAM_API = "https://api.steampowered.com"


def account_id_to_steam64(account_id: int) -> int:
    """32位 AccountID → 17位 SteamID64"""
    return STEAM_BASE + account_id


# ============================================================
# 通用 Steam API 调用
# ============================================================

def _steam_api(api_key: str, interface: str, method: str, version: int = 1,
               extra_params: dict = None, timeout: int = 15) -> dict:
    """
    统一 Steam Web API 调用。
    """
    url = f"{STEAM_API}/{interface}/{method}/v{version}/"
    params = {"key": api_key, "format": "json"}
    if extra_params:
        params.update(extra_params)

    logger = get_logger()
    try:
        resp = requests.get(url, params=params, timeout=timeout)
        if resp.status_code == 400:
            logger.info(f"Steam API {method} 返回 400（无成就或无数据）: appid={extra_params.get('appid', '?') if extra_params else '?'}")
            return {}
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        err_str = str(e)
        is_timeout = isinstance(e, requests.exceptions.Timeout) or "timed out" in err_str.lower() or "timeout" in err_str.lower()
        if is_timeout:
            logger.warn(f"Steam API {method} API请求超时，检查是否开启网络代理")
        else:
            logger.warn(f"Steam API {method} 失败: {e}")
        return {}


def _store_api(path: str, params: dict = None, timeout: int = 10) -> dict:
    """Steam Store API 通用调用。"""
    url = f"https://store.steampowered.com{path}"
    try:
        resp = requests.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger = get_logger()
        logger.warn(f"Store API {path} 失败: {e}")
        return {}


# ============================================================
# fetch_games — 获取完整的游戏库评价数据
# ============================================================

def fetch_games(steam_id_64: int, api_key: str, top_n: int = 20) -> dict:
    """
    获取用户游戏库存及关联数据，返回结构化 dict。
    """
    logger = get_logger()
    if not api_key:
        logger.warn("Steam API Key 未配置")
        return {}

    result = {}

    # ---- 1. 玩家资料 ----
    player_data = _steam_api(api_key, "ISteamUser", "GetPlayerSummaries", 2,
                             {"steamids": str(steam_id_64)})
    players = player_data.get("response", {}).get("players", [])
    if players:
        p = players[0]
        result["player"] = {
            "name": p.get("personaname", ""),
            "avatar": p.get("avatarfull", ""),
            "profile_url": p.get("profileurl", ""),
            "created": p.get("timecreated", 0),
            "country": p.get("loccountrycode", ""),
        }

    # ---- 2. 游戏库存 + 成就 ----
    owned = _steam_api(api_key, "IPlayerService", "GetOwnedGames", 1, {
        "steamid": str(steam_id_64),
        "include_played_free_games": "true",
        "include_appinfo": "true",
    })
    if not owned:
        return {}
    games_data = owned.get("response", {})
    raw_games = games_data.get("games", [])
    if not raw_games:
        logger.warn("Steam 资料为私密，无法获取游戏列表")
        return {}

    db = _load_local_db()
    total = games_data.get("game_count", len(raw_games))
    parsed = []
    tag_counter = {}
    appids_for_achieve = []

    for g in raw_games:
        name = g.get("name", "Unknown")
        appid = g.get("appid", 0)
        minutes = g.get("playtime_forever", 0)
        minutes_2w = g.get("playtime_2weeks", 0)
        hours = round(minutes / 60, 1)
        hours_2w = round(minutes_2w / 60, 1)

        if hours <= 0:
            continue

        tags = []
        if db and appid:
            game_entry = db.get("games", {}).get(str(appid), {})
            tags = game_entry.get("tags", []) if isinstance(game_entry, dict) else []

        parsed.append({
            "name": name, "appid": appid,
            "hours": hours, "hours_2w": hours_2w,
            "tags": tags, "achievements": "",
        })
        for tag in tags:
            tag_counter[tag] = tag_counter.get(tag, 0) + 1
        appids_for_achieve.append(appid)

    parsed.sort(key=lambda x: x["hours"], reverse=True)
    top_parsed = parsed[:top_n]
    total_with_playtime = len(parsed)

    # ---- 3. Top 5 游戏成就（并发获取）----
    def _fetch_achievement(aid):
        ach = _steam_api(api_key, "ISteamUserStats", "GetPlayerAchievements", 1,
                         {"steamid": str(steam_id_64), "appid": aid, "l": "schinese"})
        stats = ach.get("playerstats", {})
        if stats.get("success"):
            achievements = stats.get("achievements", [])
            unlocked = sum(1 for a in achievements if a.get("achieved") == 1)
            return (aid, f"{unlocked}/{len(achievements)}")
        return (aid, "")

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(_fetch_achievement, g["appid"]): g["appid"]
                   for g in top_parsed[:5]}
        achieve_map = {}
        for future in as_completed(futures):
            aid, ach_text = future.result()
            if ach_text:
                achieve_map[aid] = ach_text

    for g in top_parsed:
        if g["appid"] in achieve_map:
            g["achievements"] = achieve_map[g["appid"]]

    # ---- 4. 统计 ----
    gt_100h = sum(1 for x in parsed if x["hours"] >= 100)
    gt_50h = sum(1 for x in parsed if 50 <= x["hours"] < 100)
    gt_10h = sum(1 for x in parsed if 10 <= x["hours"] < 50)
    lt_10h = sum(1 for x in parsed if x["hours"] < 10)
    total_hours = sum(x["hours"] for x in parsed)
    avg_hours = round(total_hours / max(total_with_playtime, 1), 1)

    result["games"] = top_parsed
    result["stats"] = {
        "total": total, "total_with_playtime": total_with_playtime,
        "total_hours": round(total_hours), "avg_hours": avg_hours,
        "gt_100h": gt_100h, "gt_50h": gt_50h,
        "gt_10h": gt_10h, "lt_10h": lt_10h,
    }

    # ---- 5. 近期活跃 ----
    recent = _steam_api(api_key, "IPlayerService", "GetRecentlyPlayedGames", 1,
                        {"steamid": str(steam_id_64), "count": 10})
    recent_games = recent.get("response", {}).get("games", [])
    result["recent"] = [
        {"name": r.get("name", "?"), "appid": r.get("appid", 0),
         "hours_2w": round(r.get("playtime_2weeks", 0) / 60, 1),
         "hours_total": round(r.get("playtime_forever", 0) / 60, 1)}
        for r in recent_games[:8]
    ]
    recent_active = len([x for x in parsed if x["hours_2w"] > 0])
    result["recent_active_count"] = recent_active

    # ---- 6. 标签分布 ----
    sorted_tags = sorted(tag_counter.items(), key=lambda x: x[1], reverse=True)
    result["tags"] = dict(sorted_tags[:10])

    logger.info(f"Steam 数据获取完成: {total}款游戏, 成就获取{len(achieve_map)}款")
    return result


def _format_games_text(data: dict) -> str:
    """将 fetch_games() 返回的 dict 格式化为 AI prompt 文本。"""
    lines = []

    # 玩家信息
    player = data.get("player", {})
    if player:
        player_name = player.get("name", "玩家")
        created_ts = player.get("created", 0)
        created_str = time.strftime("%Y-%m-%d", time.localtime(created_ts)) if created_ts else "未知"
        lines.append(f"[玩家信息] {player_name}，Steam 账号创建于 {created_str}")

    # 库存概况
    stats = data.get("stats", {})
    lines.append(
        f"[库存概况] 共 {stats.get('total','?')} 款游戏，"
        f"有游玩时长的 {stats.get('total_with_playtime','?')} 款，"
        f"总时长 {stats.get('total_hours','?')} 小时，"
        f"平均每款 {stats.get('avg_hours','?')}h"
    )
    lines.append(
        f"[游玩分布] >100h: {stats.get('gt_100h','?')}款, "
        f"50-100h: {stats.get('gt_50h','?')}款, "
        f"10-50h: {stats.get('gt_10h','?')}款, "
        f"<10h: {stats.get('lt_10h','?')}款"
    )

    # 近期活跃
    recent = data.get("recent", [])
    recent_count = data.get("recent_active_count", 0)
    if recent_count:
        lines.append(f"[近期活跃] 近2周游玩 {recent_count} 款")
        for r in recent[:5]:
            lines.append(f"  · {r['name']} 近2周{r['hours_2w']}h（总{r['hours_total']}h）")
    else:
        lines.append("[近期活跃] 近2周无游玩记录")

    # 标签分布
    tags = data.get("tags", {})
    if tags:
        tag_list = "，".join(f"{t}({c}款)" for t, c in sorted(tags.items(), key=lambda x: x[1], reverse=True)[:10])
        lines.append(f"[标签分布] {tag_list}")

    # Top N 游戏详情
    games = data.get("games", [])
    lines.append(f"[Top {len(games)} 游戏详情]")
    for i, g in enumerate(games, 1):
        parts = [f"  {i}. {g['name']} — {g['hours']}h"]
        if g.get("hours_2w", 0) > 0:
            parts.append(f"近2周{g['hours_2w']}h")
        if g.get("achievements"):
            parts.append(f"成就{g['achievements']}")
        tags_str = "，".join(g.get("tags", [])[:4]) or "无标签"
        parts.append(f"[{tags_str}]")
        lines.append("  ".join(parts))

    return "\n".join(lines)


# ============================================================
# 游戏标签获取
# ============================================================

def _get_game_tags(appid: int) -> list:
    """通过 Steam Store API 获取游戏标签列表（genres + categories）。"""
    logger = get_logger()
    data = _store_api("/api/appdetails", {"appids": appid})
    game_data = data.get(str(appid), {}).get("data", {})
    result = []
    result.extend(g.get("description", "") for g in game_data.get("genres", []))
    result.extend(c.get("description", "") for c in game_data.get("categories", []))
    return list(filter(None, result))


# ============================================================
# recommend_games — 多源推荐引擎
# ============================================================

def recommend_games(steam_id_64: int, api_key: str, force_tag: str = "", top_n: int = 5) -> str:
    """
    基于玩家库存推荐游戏。
    返回格式化文本，失败返回空。
    """
    logger = get_logger()
    if not api_key:
        return ""

    # ---- 1. 获取库存 ----
    owned = _steam_api(api_key, "IPlayerService", "GetOwnedGames", 1, {
        "steamid": str(steam_id_64),
        "include_played_free_games": "true", "include_appinfo": "true",
    })
    games_list = owned.get("response", {}).get("games", [])
    if not games_list:
        return ""
    games_list.sort(key=lambda x: x.get("playtime_forever", 0), reverse=True)
    owned_ids = {g["appid"] for g in games_list}

    # ---- 2. 愿望单数据（排除已愿望单的游戏）----
    wishlist_ids = set()
    wishlist = _steam_api(api_key, "IWishlistService", "GetWishlist", 1,
                          {"steamid": str(steam_id_64), "count": 50})
    wl_items = wishlist.get("response", {}).get("items", [])
    if wl_items:
        wishlist_ids = {item["appid"] for item in wl_items if "appid" in item}
        logger.info(f"愿望单: {len(wishlist_ids)} 款")

    # ---- 3. 搜索关键词：优先用 Steam ML 推荐标签 ----
    search_keywords = []
    if force_tag:
        search_keywords = [force_tag]
    else:
        recommended_tags = _steam_api(api_key, "IStoreService", "GetRecommendedTagsForUser", 1,
                                      {"steamid": str(steam_id_64)})
        store_tags = recommended_tags.get("response", {}).get("tags", [])
        if store_tags:
            search_keywords = [t.get("tag", "") for t in store_tags[:5] if t.get("tag")]
            logger.info(f"Steam ML 推荐标签: {search_keywords}")
        else:
            # 回退：从 Top 10 游戏统计标签（并发获取，按 tag 累加计数）
            tag_scores = {}
            with ThreadPoolExecutor(max_workers=3) as executor:
                futures = {executor.submit(_get_game_tags, g["appid"]): g["appid"] for g in games_list[:10]}
                for future in as_completed(futures):
                    try:
                        game_tags = future.result()
                    except Exception:
                        game_tags = []
                    for tag in game_tags:
                        tag_scores[tag] = tag_scores.get(tag, 0) + 1
            search_keywords = [t[0] for t in sorted(tag_scores.items(), key=lambda x: x[1], reverse=True)[:5]]
            logger.info(f"标签统计回退: {search_keywords}")

    # ---- 4. 本地数据库查询 ----
    db = _load_local_db()
    recommended = []
    if db:
        recommended = _search_local_db(db, search_keywords, owned_ids | wishlist_ids, top_n)

    # ---- 5. Store Search 并发回退 ----
    if len(recommended) < top_n:
        seen_ids = {r[1] for r in recommended}

        def _search_single(kw):
            return _search_store(kw, 15), kw

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {executor.submit(_search_single, kw): kw for kw in search_keywords}
            for future in as_completed(futures):
                if len(recommended) >= top_n:
                    break
                results, kw = future.result()
                for name, appid in results:
                    if appid in owned_ids or appid in wishlist_ids or appid in seen_ids:
                        continue
                    seen_ids.add(appid)
                    recommended.append((name, appid, kw))
                    if len(recommended) >= top_n:
                        break

    if not recommended:
        logger.warn(f"推荐失败: 已拥有{len(owned_ids)}款, 关键词={search_keywords}")
        return ""

    lines = [f"[Steam游戏推荐] 基于玩家的游戏偏好，推荐以下 {len(recommended)} 款游戏："]
    for i, (name, appid, tag) in enumerate(recommended, 1):
        # 愿望单游戏已在上游排除，无需额外标记
        lines.append(f"  {i}. {name} (匹配标签: {tag})")
    return "\n".join(lines)


# ============================================================
# Store Search
# ============================================================

def _search_store(query: str, max_results: int = 8) -> list:
    """Steam Store 搜索，返回 [(name, appid), ...]"""
    data = _store_api("/api/storesearch/", {"term": query, "l": "zh"})
    results = data.get("items", [])[:max_results]
    return [(item.get("name", "Unknown"), item.get("id", 0)) for item in results if item.get("id")]


# ============================================================
# 本地游戏数据库
# ============================================================

# 精选 50 个热门标签。名称为 Steam 官方标签名（推荐匹配依赖名称，勿自行翻译）；
# ID 仅为兜底值——运行时通过 GetTagList 按名称动态解析，Steam 重新编号时自动跟随
STEAM_TAG_IDS = {
    "Action": 19, "Adventure": 21, "RPG": 122, "Strategy": 9,
    "Simulation": 599, "FPS": 1663, "Open World": 1695,
    "Indie": 492, "Multiplayer": 3859, "Survival": 1662,
    "Sports": 701, "Racing": 699, "Puzzle": 1664,
    "Horror": 1667, "Anime": 4085,
    "Casual": 597, "Co-op": 1685, "Sandbox": 3810,
    "Roguelike": 1716, "Roguelite": 3959,
    "Building": 1643, "Farming": 4520, "Crafting": 1702,
    "Exploration": 3834, "Shooter": 1774,
    "Platformer": 1625, "Metroidvania": 1628, "Souls-like": 29482,
    "Visual Novel": 3799, "Card Game": 1666, "Tower Defense": 1645,
    "City Builder": 4328, "Turn-Based Strategy": 1741,
    "Story Rich": 1742, "Atmospheric": 4166, "Pixel Graphics": 3964,
    "Cyberpunk": 4115, "Sci-fi": 3942, "Fantasy": 1684,
    "Zombies": 1659, "Post-apocalyptic": 3835, "Stealth": 1687,
    "Hack and Slash": 1646, "Fighting": 1743, "JRPG": 4434,
    "Action RPG": 4231, "Dungeon Crawler": 1720,
    "Psychological Horror": 1721, "Colony Sim": 220585, "Automation": 255534,
}

_LOCAL_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "steam_local_db.json")
_local_db_cache = None
# _local_db_cache 读写锁（爬取线程写、回复线程读）
_local_db_lock = threading.Lock()
_DB_MAX_PAGES = 3  # 每个标签爬取页数
_PAGE_SIZE = 25  # Steam 搜索每页条数（不足此数说明已到末页）
# 连续失败保护：连续 N 个分类爬取失败（疑似触发反爬/网络异常）时停止本次爬取，沿用上一次内容
_MAX_CONSECUTIVE_FAILURES = 3
# 标签表版本：标签集合或 ID 修正时 +1，触发全量重爬一次（旧分类内容在重爬成功前仍作兜底）
_TAG_EPOCH = 2
# Steam 官方标签列表 API（无需 Key），用于运行时按名称解析最新 tagid
_TAG_LIST_API = "https://api.steampowered.com/IStoreService/GetTagList/v1/?language=english"
_dynamic_tag_ids = None  # 运行时解析结果缓存（进程级，每次启动解析一次）
# update_local_db 模块锁，防止手动触发与自动爬取并发执行
_update_lock = threading.Lock()


def _resolve_tag_ids(logger) -> dict:
    """解析 {标签名: tagid}。

    优先调用 Steam 官方标签列表 API 按名称动态取 id（防 ID 漂移/重新编号）；
    网络失败时回退到内置 STEAM_TAG_IDS。结果进程内缓存一次。
    """
    global _dynamic_tag_ids
    if _dynamic_tag_ids is not None:
        return _dynamic_tag_ids
    try:
        resp = requests.get(_TAG_LIST_API, timeout=(8, 20))
        resp.raise_for_status()
        tags = resp.json().get("response", {}).get("tags", [])
        by_name = {t.get("name", "").lower(): t.get("tagid")
                   for t in tags if t.get("name") and t.get("tagid")}
        resolved = {}
        missing = []
        drifted = []
        for name, fallback_id in STEAM_TAG_IDS.items():
            tid = by_name.get(name.lower())
            if tid:
                resolved[name] = tid
                if tid != fallback_id:
                    drifted.append(name)
            else:
                missing.append(name)
                resolved[name] = fallback_id
        if drifted:
            logger.warn(f"[Steam 爬取] {len(drifted)} 个内置标签 ID 已被 Steam 重新编号，"
                        f"已自动使用最新 ID: {', '.join(drifted)}")
        if missing:
            logger.warn(f"[Steam 爬取] {len(missing)} 个标签未在官方列表中匹配到，"
                        f"使用内置 ID: {', '.join(missing)}")
        logger.info(f"[Steam 爬取] 标签 ID 动态解析完成（官方列表共 {len(tags)} 个标签）")
        _dynamic_tag_ids = resolved
    except Exception as e:
        logger.warn(f"[Steam 爬取] 官方标签列表获取失败，使用内置 ID 表: {e}")
        _dynamic_tag_ids = dict(STEAM_TAG_IDS)
    return _dynamic_tag_ids


def _scrape_tag_page(tag_id: int, tag_name: str, max_pages: int = _DB_MAX_PAGES) -> tuple:
    """爬取 Steam 标签搜索页。

    返回 (results, responded)：results 为 [(name, appid), ...]；
    responded 表示是否至少成功取回一个页面（用于区分"网络失败"与"页面可访问但无结果，
    后者通常意味着标签 ID 已失效"）。
    """
    logger = get_logger()
    results = []
    responded = False
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    for page in range(1, max_pages + 1):
        url = f"https://store.steampowered.com/search/?tags={tag_id}&ndl=1&page={page}"
        html = ""
        # 失败重试 2 次。读超时给足 25s：搜索页 HTML 约 600KB，代理/慢网络下 10s 经常不够
        for attempt in range(2):
            try:
                resp = requests.get(url, headers=headers, timeout=(8, 25))
                resp.raise_for_status()
                html = resp.text
                responded = True
                break
            except Exception as e:
                logger.warn(f"[Steam 爬取] {tag_name} 第{page}页第{attempt + 1}次请求失败: {e}")
                if attempt == 0:
                    time.sleep(1.0 + random.uniform(0.5, 1.0))
                continue

        if not html:
            continue

        pattern = r'data-ds-appid="(\d+)".*?<span class="title">([^<]+)</span>'
        matches = re.findall(pattern, html, re.DOTALL)
        if not matches:
            break
        for appid, name in matches:
            name = name.strip()
            if name and appid:
                results.append((name, int(appid)))
        if len(matches) < _PAGE_SIZE:
            break
        # 降低请求频率并加入随机抖动，避免被 Steam 反爬
        time.sleep(1.5 + random.uniform(0.5, 1.0))
    return results, responded


def get_scrape_status(log_output: bool = False) -> list:
    """获取 Steam 游戏榜单各分类的时效状态。

    返回每个分类的字典列表：tag, date, count, need_update。
    log_output=True 时将所有分类状态折叠到一条日志中输出。
    """
    logger = get_logger()
    today = time.strftime("%Y-%m-%d")

    db = {}
    try:
        with open(_LOCAL_DB_PATH, "r", encoding="utf-8") as f:
            db = json.load(f)
    except Exception as e:
        print(f"[Steam] 读取本地游戏库失败（时效检测按未爬取处理）: {e}")

    tag_updated_at = db.get("tag_updated_at", {})
    by_tag = db.get("by_tag", {})

    status_list = []
    summary_parts = []
    for tag_name in STEAM_TAG_IDS.keys():
        date_str = tag_updated_at.get(tag_name, "")
        date_display = date_str.replace("-", ".") if date_str else "未爬取"
        count = len(by_tag.get(tag_name, []))
        need_update = date_str != today
        status_text = "需更新" if need_update else "无需更新"

        summary_parts.append(f"{tag_name}:{date_display}/{count}/{status_text}")

        status_list.append({
            "tag": tag_name,
            "date": date_str or "",
            "count": count,
            "need_update": need_update,
        })

    if log_output:
        logger.info("[自检] 正在检测 Steam 游戏榜单时效... " + ", ".join(summary_parts))

    return status_list


def _save_local_db(games: dict, by_tag: dict, tag_updated_at: dict) -> bool:
    """将当前进度持久化到 data/steam_local_db.json。"""
    logger = get_logger()
    global _local_db_cache

    cleaned_games = {appid: info for appid, info in games.items() if info.get("tags")}
    db = {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tag_epoch": _TAG_EPOCH,
        "tag_updated_at": tag_updated_at,
        "games": {str(k): v for k, v in cleaned_games.items()},
        "by_tag": {k: list(v) for k, v in by_tag.items()},
    }

    try:
        os.makedirs(os.path.dirname(_LOCAL_DB_PATH), exist_ok=True)
        atomic_write_json(_LOCAL_DB_PATH, db)
        with _local_db_lock:
            _local_db_cache = db
        return True
    except Exception as e:
        logger.error(f"[Steam 爬取] 保存失败: {e}")
        return False


def update_local_db(force: bool = False) -> bool:
    """爬取 Steam 热门标签游戏并存入本地 data/steam_local_db.json。

    每个分类独立判断：若该分类今天已更新过，则直接复用本地数据，
    否则执行爬取。force=True 时强制重新爬取所有分类。
    每个分类处理完成后立即保存，避免中途中断导致重复爬取。
    已有爬取在进行中时直接返回 False。
    """
    logger = get_logger()

    if not _update_lock.acquire(blocking=False):
        logger.warn("[Steam 爬取] 已有爬取任务在进行中，本次跳过")
        return False

    try:
        return _update_local_db_inner(logger, force)
    finally:
        _update_lock.release()


def _update_local_db_inner(logger, force: bool) -> bool:
    """update_local_db 的实际实现（调用方需持有 _update_lock）。"""
    today = time.strftime("%Y-%m-%d")

    # 加载已有数据（如果存在）
    existing = {"games": {}, "by_tag": {}, "tag_updated_at": {}}
    try:
        with open(_LOCAL_DB_PATH, "r", encoding="utf-8") as f:
            existing = json.load(f)
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warn(f"[Steam 爬取] 读取本地数据库失败，将重新爬取: {e}")

    # 单个键转换失败跳过，不炸整个函数
    games = {}
    for k, v in existing.get("games", {}).items():
        try:
            games[int(k)] = v
        except (ValueError, TypeError):
            logger.warn(f"[Steam 爬取] 本地数据库存在无效 appid 键: {k!r}，已跳过")
    by_tag = {k: list(v) for k, v in existing.get("by_tag", {}).items()}
    tag_updated_at = existing.get("tag_updated_at", {})

    # 标签表已换代（精选 50 标签 / 修正被 Steam 重新编号的 ID）：
    # 作废所有分类的"已更新"标记，强制全量重爬一次；旧内容在重爬成功前仍作兜底
    if existing.get("tag_epoch") != _TAG_EPOCH:
        logger.info("[Steam 爬取] 标签表已更新，本次将重新爬取所有分类")
        tag_updated_at = {}

    tags = _resolve_tag_ids(logger)
    skipped_tags = []
    updated_tags = []
    failed_tags = []
    consecutive_failures = 0
    aborted = False

    for tag_name, tag_id in tags.items():
        # 非强制模式下，今日已更新过的分类直接跳过
        if not force and tag_updated_at.get(tag_name) == today:
            skipped_tags.append(tag_name)
            # 确保跳过的分类数据被重建到 games 中
            for appid in by_tag.get(tag_name, []):
                appid_int = int(appid)
                game = games.get(appid_int)
                if game and tag_name not in game.get("tags", []):
                    game.setdefault("tags", []).append(tag_name)
            continue

        logger.info(f"[Steam 爬取] {tag_name}...")
        results, responded = _scrape_tag_page(tag_id, tag_name)

        # 0 结果不清数据：保留旧分类内容，不标记已更新、不保存
        if not results:
            consecutive_failures += 1
            failed_tags.append(tag_name)
            reason = ("页面可访问但无结果，标签 ID 可能已失效" if responded
                      else "网络请求失败")
            logger.warn(f"[Steam 爬取] {tag_name}: {reason}（连续失败 {consecutive_failures}/{_MAX_CONSECUTIVE_FAILURES}），保留旧数据")
            if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                aborted = True
                logger.warn(f"[Steam 爬取] 已连续 {_MAX_CONSECUTIVE_FAILURES} 个分类爬取失败，疑似触发反爬或网络异常，停止本次爬取，剩余分类沿用上一次的内容")
                break
            continue

        consecutive_failures = 0

        # 清空旧分类数据，使用新结果
        by_tag[tag_name] = []
        for appid in list(games.keys()):
            if tag_name in games[appid].get("tags", []):
                games[appid]["tags"].remove(tag_name)

        for name, appid in results:
            appid_int = int(appid)
            if appid_int not in games:
                games[appid_int] = {"name": name, "tags": []}
            if tag_name not in games[appid_int]["tags"]:
                games[appid_int]["tags"].append(tag_name)
            by_tag[tag_name].append(appid_int)

        # 输出每个标签的爬取汇总
        logger.info(f"[Steam 爬取] {tag_name}: {len(results)} 款游戏")

        tag_updated_at[tag_name] = today
        updated_tags.append(tag_name)

        # 每个分类完成后立即保存，避免中断后重复爬取
        if _save_local_db(games, by_tag, tag_updated_at):
            logger.info(f"[Steam 爬取] {tag_name}: 已保存进度")
        else:
            logger.error(f"[Steam 爬取] {tag_name}: 进度保存失败")

        # 标签之间加入随机间隔
        time.sleep(1.0 + random.uniform(0.5, 1.0))

    # 汇总跳过/更新情况，避免刷屏
    summary_parts = []
    if updated_tags:
        summary_parts.append(f"更新 {len(updated_tags)} 个分类")
    if skipped_tags:
        summary_parts.append(f"跳过 {len(skipped_tags)} 个分类")
    if failed_tags:
        summary_parts.append(f"失败 {len(failed_tags)} 个分类（沿用旧数据）")
    if aborted:
        summary_parts.append("连续失败已提前停止")
    summary_str = ", ".join(summary_parts) if summary_parts else "无变化"
    logger.info(f"[Steam 爬取] 完成：{summary_str}，共 {len(games)} 款游戏")
    return True


def _load_local_db() -> dict:
    global _local_db_cache
    with _local_db_lock:
        if _local_db_cache is not None:
            return _local_db_cache
        try:
            with open(_LOCAL_DB_PATH, "r", encoding="utf-8") as f:
                _local_db_cache = json.load(f)
            return _local_db_cache
        except Exception as e:
            get_logger().warn(f"读取本地游戏库失败: {e}")
            return {}


def _search_local_db(db: dict, tags: list, exclude_ids: set, top_n: int = 5) -> list:
    """从本地数据库查询推荐游戏。返回 [(name, appid, matched_tag), ...]"""
    recommended = []
    seen = set()
    for tag in tags:
        appids = db.get("by_tag", {}).get(tag, [])
        for appid in appids:
            appid_int = int(appid) if isinstance(appid, str) else appid
            if appid_int in exclude_ids or appid_int in seen:
                continue
            seen.add(appid_int)
            game = db.get("games", {}).get(str(appid), {})
            name = game.get("name", "Unknown") if isinstance(game, dict) else str(game)
            recommended.append((name, appid_int, tag))
            if len(recommended) >= top_n:
                return recommended
    return recommended