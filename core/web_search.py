"""
联网搜索模块
使用百度千帆 AI 搜索 API（v2/ai_search/chat/completions）
"""

import requests

from config.config_manager import get_config
from logger.log_manager import get_logger
from .stats import get_stats

BAIDU_SEARCH_URL = "https://qianfan.baidubce.com/v2/ai_search/chat/completions"

# 搜索结果前缀标记（bot_engine 依赖此前缀剥离搜索结果）
SEARCH_PREFIX = "[SEARCH]"


def search_web(query: str, max_results: int = 5) -> str:
    """
    根据配置的搜索提供商调用对应的搜索 API。
    返回格式化文本，失败时返回空字符串。
    """
    if not query or not query.strip():
        return ""

    logger = get_logger()
    config = get_config()
    baidu_config = config.get_llm_baidu_search_config()
    provider = baidu_config.get("provider", "baidu")

    # 确认 provider key 已配置后才统计搜索调用（未配置不计数）
    baidu_key = baidu_config.get("baidu_api_key", "")
    tavily_key = baidu_config.get("tavily_api_key", "")
    key_configured = (
        (provider == "baidu" and baidu_key)
        or (provider == "tavily" and tavily_key)
        or (provider == "auto" and (baidu_key or tavily_key))
    )
    if key_configured:
        get_stats().record_search()

    if provider == "tavily":
        return _search_tavily(query, baidu_config, max_results, logger)
    elif provider == "auto":
        # 自动故障转移：优先百度，失败则回退 Tavily
        result = _search_baidu(query, baidu_config, max_results, logger)
        if result:
            return result
        logger.info("百度搜索未返回结果，尝试 Tavily 回退搜索...")
        return _search_tavily(query, baidu_config, max_results, logger)
    else:
        return _search_baidu(query, baidu_config, max_results, logger)


def _search_baidu(query: str, baidu_config: dict, max_results: int, logger) -> str:
    """百度千帆 AI 搜索 API。"""
    api_key = baidu_config.get("baidu_api_key", "")
    model = baidu_config.get("model", "deepseek-v3")

    if not api_key:
        logger.warn("百度搜索 API Key 未配置，跳过搜索")
        return ""

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    payload = {
        "messages": [{"content": query.strip(), "role": "user"}],
        "stream": False,
        "model": model,
        "instruction": "请基于搜索结果直接输出相关信息的摘要，不要添加额外解释。",
        "enable_corner_markers": False,
        "enable_deep_search": True,
    }

    try:
        resp = requests.post(BAIDU_SEARCH_URL, headers=headers, json=payload, timeout=120)
        if resp.status_code == 200:
            data = resp.json()
            # 类型安全解析：result 只接受 str，dict/list 一律视为无结果
            result = ""
            choices = data.get("choices")
            if isinstance(choices, list) and choices:
                first = choices[0] if isinstance(choices[0], dict) else {}
                content = first.get("message", {}).get("content", "")
                if isinstance(content, str):
                    result = content
            if not result:
                r = data.get("result", "")
                if isinstance(r, str):
                    result = r
            if not result:
                data_field = data.get("data")
                if isinstance(data_field, dict):
                    r = data_field.get("result", "")
                    if isinstance(r, str):
                        result = r
                elif isinstance(data_field, str):
                    result = data_field

            if result:
                logger.info(f"百度搜索成功: 查询={query[:50]}, 模型={model}")
                # 按字符数限制搜索结果：匹配 Tavily 端 max_results 条 × 300 字符 ≈ 1500 字符
                max_chars = max_results * 300
                if len(result) > max_chars:
                    result = result[:max_chars] + "..."
                return SEARCH_PREFIX + "[百度AI搜索结果]\n" + result
            else:
                logger.warn(f"百度搜索返回空内容，响应: {str(data)[:200]}")

        elif resp.status_code == 401 or resp.status_code == 403:
            logger.error("百度搜索 API Key 无效或权限不足")
        else:
            logger.warn(f"百度搜索返回 HTTP {resp.status_code}: {resp.text[:200]}")

    except requests.RequestException as e:
        logger.warn(f"百度搜索网络错误: {e}")
    except (AttributeError, TypeError, KeyError, ValueError) as e:
        logger.warn(f"百度搜索响应解析失败: {e}")

    return ""


def _search_tavily(query: str, baidu_config: dict, max_results: int, logger) -> str:
    """Tavily 搜索 API。"""
    api_key = baidu_config.get("tavily_api_key", "")

    if not api_key:
        logger.warn("Tavily API Key 未配置，跳过搜索")
        return ""

    try:
        from tavily import TavilyClient
        client = TavilyClient(api_key=api_key)
        response = client.search(query.strip())
        results = response.get("results", [])
        if results:
            logger.info(f"Tavily 搜索成功: 查询={query[:50]}, 结果数={len(results)}")
            lines = ["[联网搜索结果]"]
            for i, r in enumerate(results[:max_results]):
                title = r.get("title", "")
                content = r.get("content", "")
                url = r.get("url", "")
                line = f"{i + 1}. {title}"
                if content:
                    line += f" -- {content[:300]}"
                if url:
                    line += f" [来源: {url}]"
                lines.append(line)
            return SEARCH_PREFIX + "\n".join(lines)
        else:
            logger.warn("Tavily 搜索未返回结果")
    except ImportError:
        logger.error("Tavily 库未安装，请执行: pip install tavily-python")
    except Exception as e:
        logger.warn(f"Tavily 搜索异常: {e}")

    return ""
