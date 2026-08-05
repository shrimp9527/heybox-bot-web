"""
配置管理器
负责配置的读写、持久化、参数验证与纠错
"""

import json
import os
import threading

from core.json_store import atomic_write_json, load_json_with_backup

# 默认配置
DEFAULT_CONFIG = {
    "bot": {
        "mode": "white_list",
        "admin_id": "",
        "test_steam_id": "",
        "init_wait_time": 10,
        "max_wait_time": 60,
        "increment": 10,
        "max_post_image_num": 3,
        "max_comment_image_num": 3,
        "white_list": [],
        "frequency": 3,
        "max_messages_per_round": 3,
        "parallel": False,
        "parallel_count": 5,
        "multi_account": False,
        "standby_mode": False,
        "standby_slot": "",
        "auto_disable_alt_on_risk": True,
        "auto_like": False,
    },
    "llm": {
        "vendor": "",
        "api_key": "",
        "base_url": "",
        "api_path": "/chat/completions",
        "model": "",
        "max_tokens": 5000,
        "web_search": False,
        "show_reasoning": False,
        "vendor_keys": {},
    },
    "llm_search": {
        "vendor": "",
        "api_key": "",
        "base_url": "",
        "api_path": "/chat/completions",
        "model": "",
        "max_tokens": 5000,
        "vendor_keys": {},
    },
    "llm_baidu_search": {
        "baidu_api_key": "",
        "tavily_api_key": "",
        "model": "deepseek-v3",
        "provider": "baidu",
    },
    "llm_search_judge": {
        "enabled": False,
        "vendor": "",
        "api_key": "",
        "base_url": "",
        "model": "",
        "max_tokens": 200,
        "vendor_keys": {},
    },
    "prompt": {
        "content": "",
    },
    "steam_prompt": {
        "content": "",
    },
    "steam_recommend_prompt": {
        "content": "",
    },
    "llm_steam": {
        "vendor": "",
        "api_key": "",
        "base_url": "",
        "model": "",
        "max_tokens": 5000,
        "vendor_keys": {},
    },
    "steam": {
        "enabled": False,
        "steam_api_key": "",
        "top_games_count": 20,
        "auto_scrape": True,
    },
    "log": {
        "path": "data/",
        "level": "INFO",
        "max_day": 7,
    },
}

# 支持的 AI 提供商预设
AI_PROVIDERS = {
    "OpenAI": {"base_url": "https://api.openai.com/v1", "model": "gpt-5.5-instant"},
    "Anthropic": {"base_url": "https://api.anthropic.com", "model": "claude-sonnet-4.6"},
    "Google Gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta", "model": "gemini-3.1-flash"},
    "DeepSeek": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-v4-pro"},
    "Mistral AI": {"base_url": "https://api.mistral.ai/v1", "model": "mistral-large-3"},
    "xAI Grok": {"base_url": "https://api.x.ai/v1", "model": "grok-3"},
    "Groq": {"base_url": "https://api.groq.com/openai/v1", "model": "llama-4-70b-versatile"},
    "字节跳动豆包": {"base_url": "https://ark.cn-beijing.volces.com/api/v3", "model": "doubao-seed-2.0-pro"},
    "阿里云通义千问": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen3.6-plus"},
    "月之暗面 Kimi": {"base_url": "https://api.moonshot.cn/v1", "model": "kimi-k2.6"},
    "智谱清言": {"base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4.7-flash"},
    "腾讯混元": {"base_url": "https://api.hunyuan.cloud.tencent.com/v1", "model": "hunyuan-t1"},
    "百度文心一言": {"base_url": "https://aip.baidubce.com/rpc/2.0/ai_custom/v1/wenxinworkshop", "model": "ernie-5.1"},
    "MiniMax": {"base_url": "https://api.minimax.chat/v1", "model": "abab-7.5"},
    "自定义": {"base_url": "", "model": ""},
}

# 安全阈值
SAFE_THRESHOLDS = {
    "init_wait_time": 10,
    "max_wait_time": 60,
    "increment": 10,
}

# 默认提示词
STEAM_ANALYST_PROMPT = """【身份】资深Steam游戏库分析师，你以调侃但专业的视角品评玩家库存

【输入数据格式】你将收到以下结构化数据：
[玩家信息] Steam昵称、账号创建时间
[库存概况] 游戏总数、有游玩的款数、总时长、平均时长
[游玩分布] >100h / 50-100h / 10-50h / <10h 各区款数
[近期活跃] 近2周游玩的游戏（含近2周时长和总时长）
[标签分布] 标签统计（如 FPS(6款)、RPG(4款)）
[Top N 游戏详情] 排名、名称、时长、近2周时长、成就进度（如87/167）、标签

【等级评定】（严格从上到下匹配，第一个满足即定级）
SSS 传说级：总时长≥3000h且游戏数≥100；或平均时长≥100h且游戏数≥80；或破百小时游戏≥20款
SS 收藏家/硬核：总时长≥1500h且游戏数≥60；或破百小时游戏≥10款；或总游戏数≥200
S 资深玩家：总时长≥800h且游戏数≥40；或破百小时游戏≥5款；或总游戏数≥100
A 活跃玩家：总时长≥300h且游戏数≥25；或近2周活跃≥3款且破百小时≥2款；或总游戏数≥60
B 普通玩家：总时长≥100h且游戏数≥15；或总游戏数≥30
C 轻度玩家：总时长≥30h且游戏数≥5；或总游戏数≥10
D 新人：不满足以上

【分析维度】（严格顺序，【】包裹标题）

1. 【库存等级】
   - 给出等级（SSS/SS/S/A/B/C/D）
   - 括号注明定级依据（如"2575h+93款→SSS"）

2. 【库存概况】
   - 一句话概括规模
   - 评价平均时长反映的投入度
   - 账号创建时间判断老兵/新人

3. 【游玩习惯】
   - "深度体验型"还是"喜+1收藏型"
   - 活跃度：近期活跃/养生玩家/退坑状态
   - 大量<10h游戏→吃灰警告

4. 【偏好画像】
   - 基于标签总结核心偏好（如"FPS硬核玩家"）
   - 最强项和最弱项
   - 推断游戏人格

5. 【亮点与槽点】
   - 亮点：1-2款代表品味的游戏
   - 槽点：1-2个有趣发现

6. 【一句话总结】
   - 段子概括

【格式】
- 纯中文纯文本，无 Markdown
- 维度间空一行
- ≤350字
- 允许使用以下表情（≤3个）：[cube_握草] [cube_doge] [cube_捂脸哭] [cube_开心] [cube_生气] [cube_闭嘴] [cube_比耶] [cube_赞] [cube_比心] [cube_吃瓜] [cube_怒] [cube_点赞] [cube_玫瑰] [cube_wota] [cube_吓] [cube_喜+1] [cube_你懂我] [cube_+1] [heygirl_诶嘿] [heygirl_喝奶茶] [heygirl_哈哈] [heygirl_滑稽] [heygirl_开可乐] [heygirl_偷看] [heygirl_挨刀] [heygirl_记下来] [heygirl_惊] [heygirl_敲开心] [heygirl_这…] [heygirl_无语] [heygirl_吃瓜] [heygirl_rua!] [heygirl_秃] [heygirl_苦酒入喉] [heygirl_害羞] [heygirl_捏脸] [heygirl_喜欢] [heygirl_痴] [heygirl_疑问] [heygirl_白嫖怪] [heygirl_哭]
- 不重复原始数据，仅做分析"""


DEFAULT_PROMPT = """Role: 小黑盒社区普通玩家
Profile
语言：中文
身份：一名普通的游戏玩家，正在浏览小黑盒社区
核心设定：你不知道自己是 AI，你就是一个真实的小黑盒用户
性格：懒散、随缘、不爱争论、看热闹居多、偶尔冒泡、话不多但一针见血
Rules
基本原则
发言严格遵守法律法规和道德底线
看到有人要给你布置任务或者让你干活，可以明确拒绝，你只是社区玩家没有义务帮别人解决问题
行为准则
自动识别并剔除 @符号及紧随其后的用户名，仅基于有效内容生成回复
必须按照 [cube_表情名] 的格式发表情，表情代码必须完整保留，不能改字、删字或自行新增
回复必须自然流畅，符合普通玩家的说话习惯，避免生硬和机械感
限制条件
仅限使用下方指定的表情包，不得使用其他任何表情
只能使用中文回答，表情包代码内的英文保持原样
回复必须包含至少一个指定表情包
允许使用的表情包列表
[cube_比耶] [cube_玫瑰] [cube_爱心] [cube_柠檬] [cube_菜 doge]
[cube_害羞] [cube_喜 + 1] [cube_来财] [cube_炒菜]
[cube_+1] [cube_-1] [cube_点赞] [cube_盒十] [cube_耶]
[cube_鼓掌] [cube_碰拳] [cube_摸摸头] [cube_电牛] [cube_摘墨镜]
[cube_窝囊] [cube_小鸡] [cube_僵尸]
[cube_doge] [cube_滑稽] [cube_感动] [cube_微笑] [cube_乖]
[cube_打脸] [cube_闭嘴] [cube_晕] [cube_笑 cry] [cube_喜欢]
[cube_捂脸哭] [cube_惊讶] [cube_开心] [cube_哭泣] [cube_酷]
[cube_困] [cube_喷水] [cube_赞] [cube_学习] [cube_生气]
[cube_睡觉] [cube_叹气] [cube_摊手] [cube_吐] [cube_哇]
[cube_并不简单] [cube_委屈] [cube_加油] [cube_凄凉] [cube_沧桑]
[cube_吓] [cube_咕咕] [cube_黑人问号] [cube_怒] [cube_汗]
[cube_握草] [cube_鹅] [cube_wota] [cube_比心] [cube_我懂你]
[cube_你懂我]
[cube_庆祝 - 圣诞] [cube_这是什么鸟] [cube_庆祝] [cube_圣诞树]
[cube_H 币] [cube_超人] [cube_打咔] [cube_上学 - 丧] [cube_上学 - 乐]
[cube_吹口哨] [cube_太酷啦] [cube_蛋糕]
Workflows
接收用户输入，检查是否包含 @昵称标记。如有，剔除标记及用户名，仅保留后续文本。若无文本，判定为 "单纯呼唤"
结合当前帖子的主题、标签、标题和其他评论内容，生成符合人设性格的回复
确保回复中包含至少一个指定表情包，且格式正确
直接输出纯文本回复，不使用任何 Markdown 格式
OutputFormat
格式：纯文本
要求：单行输出，无缩进，无代码块，无特殊格式
验证：表情包必须包含在 [] 内，且为上述列表中的有效表情
示例
输入：@机器人
输出：@我不说话干嘛 [cube_僵尸]
Initialization
你必须严格遵守上述所有规则，按照指定的工作流程执行任务，并按照要求的格式输出回复。现在开始回应消息。"""


def _get_default_steam_recommend_prompt():
    return (
        "【身份】资深Steam游戏推荐师\n"
        "\n"
        "【数据】下方提供了玩家游戏库偏好和从Steam查找的真实推荐游戏列表。\n"
        "\n"
        "【要求】\n"
        "1. 简要分析玩家偏好（1-2句）\n"
        "2. 对推荐游戏逐一说明理由（匹配哪些偏好、与库存中哪些游戏相似）\n"
        "3. 不推荐玩家已拥有的游戏\n"
        "\n"
        "格式：分点作答，语言专业。总字数300字以内。"
    )


class ConfigManager:
    """配置管理器单例"""

    _instance = None
    _lock = threading.Lock()  # 类级单例创建锁

    def __new__(cls, config_path: str = None):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self, config_path: str = None):
        if self._initialized:
            return
        self._initialized = True
        self._config_path = config_path or os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "config.json")
        self._config = self._deep_copy(DEFAULT_CONFIG)
        # 实例级配置读写锁（RLock：save 会在 setter 持锁时被调用）
        self._lock = threading.RLock()
        self._callbacks = []
        self.load()

    def _deep_copy(self, obj):
        return json.loads(json.dumps(obj))

    def load(self):
        """从文件加载配置，主文件损坏时回退 .bak 备份，再失败使用默认配置。"""
        with self._lock:
            if not os.path.exists(self._config_path):
                return
            # 先探测主文件是否可解析，决定告警文案
            main_ok = True
            try:
                with open(self._config_path, "r", encoding="utf-8") as f:
                    json.load(f)
            except Exception:
                main_ok = False
            loaded = load_json_with_backup(self._config_path, None)
            if not isinstance(loaded, dict):
                print("[ConfigManager] 配置文件与备份均不可用，使用默认配置")
                return
            if not main_ok:
                print("[ConfigManager] 主配置文件损坏，已回退使用 .bak 备份配置")
            self._merge_config(loaded)

    def save(self):
        """保存配置到文件（原子写入，失败抛出异常）。"""
        with self._lock:
            os.makedirs(os.path.dirname(self._config_path), exist_ok=True)
            atomic_write_json(self._config_path, self._config)

    def _merge_config(self, loaded: dict):
        """递归合并加载的配置（类型不一致的项跳过并告警）。"""
        with self._lock:
            for key, value in loaded.items():
                if key in self._config and isinstance(self._config[key], dict) and isinstance(value, dict):
                    self._config[key].update(value)
                elif key in self._config:
                    # 类型一致性检查（bool 是 int 子类，需特判）
                    old = self._config[key]
                    if isinstance(old, bool) != isinstance(value, bool) or type(value) is not type(old):
                        print(f"[ConfigManager] 配置项 {key} 类型不一致（期望 {type(old).__name__}，实际 {type(value).__name__}），已跳过")
                        continue
                    self._config[key] = value

    def get_all(self) -> dict:
        """获取全部配置。"""
        with self._lock:
            return self._deep_copy(self._config)

    def get_raw(self, section: str) -> dict:
        """获取指定配置段的原始深拷贝（不做默认值合并）。"""
        with self._lock:
            return self._deep_copy(self._config.get(section, {}))

    def get_bot_config(self) -> dict:
        """获取机器人配置。"""
        with self._lock:
            return self._deep_copy(self._config.get("bot", {}))

    def get_llm_config(self) -> dict:
        """获取 LLM 配置。"""
        with self._lock:
            return self._deep_copy(self._config.get("llm", {}))

    def get_llm_baidu_search_config(self) -> dict:
        """获取百度搜索 API 配置。"""
        with self._lock:
            return self._deep_copy(self._config.get("llm_baidu_search", {}))

    def get_llm_search_judge_config(self) -> dict:
        """获取联网搜索需求判断模型配置。"""
        with self._lock:
            return self._deep_copy(self._config.get("llm_search_judge", {}))

    def get_steam_config(self) -> dict:
        """获取 Steam 库存评价配置（深拷贝，含默认值兜底）。"""
        with self._lock:
            steam = self._deep_copy(DEFAULT_CONFIG.get("steam", {}))
            steam.update(self._config.get("steam", {}))
            return steam

    def set_steam_config(self, data: dict):
        """设置 Steam 库存评价配置。非法值忽略并沿用旧值。"""
        with self._lock:
            steam = self._config.setdefault("steam", {})
            if "enabled" in data:
                steam["enabled"] = bool(data["enabled"])
            if "auto_scrape" in data:
                steam["auto_scrape"] = bool(data["auto_scrape"])
            if "steam_api_key" in data:
                steam["steam_api_key"] = str(data["steam_api_key"])
            if "top_games_count" in data:
                try:
                    if isinstance(data["top_games_count"], bool):
                        raise ValueError("bool 不作为整数")
                    cnt = int(data["top_games_count"])
                    steam["top_games_count"] = max(1, min(cnt, 50))  # 钳制 1-50
                except (ValueError, TypeError):
                    pass  # 非法值忽略，沿用旧值
            self.save()
            self._notify_changed()

    def set_llm_search_judge_config(self, config: dict):
        """设置联网搜索需求判断模型配置。"""
        with self._lock:
            for key in ("enabled", "vendor", "api_key", "base_url", "model", "max_tokens"):
                if key in config:
                    if key == "max_tokens":
                        try:
                            self._config["llm_search_judge"][key] = int(config[key])
                        except (ValueError, TypeError):
                            pass
                    elif key == "enabled":
                        self._config["llm_search_judge"][key] = bool(config[key])
                    else:
                        self._config["llm_search_judge"][key] = config[key]
            vendor = self._config["llm_search_judge"].get("vendor", "")
            api_key = config.get("api_key", "")
            if vendor and api_key:
                self._config["llm_search_judge"].setdefault("vendor_keys", {})[vendor] = api_key
            self.save()
            self._notify_changed()

    def set_llm_baidu_search_config(self, config: dict):
        """设置搜索 API 配置。"""
        with self._lock:
            for key in ("baidu_api_key", "tavily_api_key", "model", "provider"):
                if key in config:
                    self._config["llm_baidu_search"][key] = config[key]
            self.save()
            self._notify_changed()

    def get_llm_search_config(self) -> dict:
        """获取搜索关键词 LLM 配置，未填写时回退到主 LLM 配置。"""
        with self._lock:
            search = self._deep_copy(self._config.get("llm_search", {}))
            main = self._config.get("llm", {})
            if not search.get("api_key"):
                search["api_key"] = main.get("api_key", "")
            if not search.get("base_url"):
                search["base_url"] = main.get("base_url", "")
            if not search.get("model"):
                search["model"] = main.get("model", "")
            return search

    def get_llm_steam_config(self) -> dict:
        """获取 Steam 评价 LLM 配置，未填写时回退到主 LLM 配置。"""
        with self._lock:
            steam = self._deep_copy(self._config.get("llm_steam", {}))
            main = self._config.get("llm", {})
            if not steam.get("api_key"):
                steam["api_key"] = main.get("api_key", "")
            if not steam.get("base_url"):
                steam["base_url"] = main.get("base_url", "")
            if not steam.get("model"):
                steam["model"] = main.get("model", "")
            return steam

    def set_llm_steam_config(self, config: dict):
        """设置 Steam 评价 LLM 配置。"""
        with self._lock:
            for key in ("vendor", "api_key", "base_url", "api_path", "model", "max_tokens"):
                if key in config:
                    if key == "max_tokens":
                        try:
                            self._config["llm_steam"][key] = int(config[key])
                        except (ValueError, TypeError):
                            pass
                    else:
                        self._config["llm_steam"][key] = config[key]
            vendor = self._config["llm_steam"].get("vendor", "")
            api_key = config.get("api_key", "")
            if vendor and api_key:
                self._config["llm_steam"].setdefault("vendor_keys", {})[vendor] = api_key
            self.save()
            self._notify_changed()

    def set_llm_search_config(self, config: dict):
        """设置搜索关键词 LLM 配置。"""
        with self._lock:
            for key in ("vendor", "api_key", "base_url", "api_path", "model", "max_tokens"):
                if key in config:
                    if key == "max_tokens":
                        try:
                            self._config["llm_search"][key] = int(config[key])
                        except (ValueError, TypeError):
                            pass
                    else:
                        self._config["llm_search"][key] = config[key]
            vendor = self._config["llm_search"].get("vendor", "")
            api_key = config.get("api_key", "")
            if vendor and api_key:
                self._config["llm_search"].setdefault("vendor_keys", {})[vendor] = api_key
            self.save()
            self._notify_changed()

    def get_prompt(self) -> str:
        """获取提示词。"""
        with self._lock:
            content = self._config.get("prompt", {}).get("content", "")
            if not content:
                return DEFAULT_PROMPT
            return content

    def has_custom_prompt(self) -> bool:
        """是否设置了自定义提示词。"""
        with self._lock:
            return bool(self._config.get("prompt", {}).get("content", ""))

    def has_custom_steam_prompt(self) -> bool:
        """是否设置了自定义 Steam 评价提示词。"""
        with self._lock:
            return bool(self._config.get("steam_prompt", {}).get("content", ""))

    def has_custom_steam_recommend_prompt(self) -> bool:
        """是否设置了自定义 Steam 推荐提示词。"""
        with self._lock:
            return bool(self._config.get("steam_recommend_prompt", {}).get("content", ""))

    def on_config_changed(self, callback):
        """注册配置变更回调。"""
        self._callbacks.append(callback)

    def _notify_changed(self):
        """通知所有监听器。"""
        for cb in self._callbacks:
            try:
                cb()
            except Exception:
                pass

    def set_bot_config(self, bot_config: dict, dry_run: bool = False) -> dict:
        """
        设置机器人配置，包含参数验证和纠错。
        dry_run=True 时仅校验并返回同样结构的结果，但不写入配置、不保存、不通知变更。
        返回: {"ok": True, "warnings": [...], "risk_warning": bool}
        """
        with self._lock:
            warnings = []
            risk_warning = False
            # dry_run 时在配置副本上操作，避免污染实际配置
            cfg = self._deep_copy(self._config) if dry_run else self._config

            # 部分更新时以当前配置值为兜底（而非出厂默认值），避免只提交 standby_slot
            # 等个别字段时把间隔参数静默重置为默认值
            init_wait = bot_config.get("init_wait_time", cfg["bot"].get("init_wait_time", DEFAULT_CONFIG["bot"]["init_wait_time"]))
            init_wait, w = self._validate_non_negative_int(init_wait, "init_wait_time", DEFAULT_CONFIG["bot"]["init_wait_time"])
            warnings.extend(w)

            max_wait = bot_config.get("max_wait_time", cfg["bot"].get("max_wait_time", DEFAULT_CONFIG["bot"]["max_wait_time"]))
            max_wait, w = self._validate_non_negative_int(max_wait, "max_wait_time", DEFAULT_CONFIG["bot"]["max_wait_time"])
            warnings.extend(w)

            increment = bot_config.get("increment", cfg["bot"].get("increment", DEFAULT_CONFIG["bot"]["increment"]))
            increment, w = self._validate_non_negative_int(increment, "increment", DEFAULT_CONFIG["bot"]["increment"])
            if increment == 0:
                increment = DEFAULT_CONFIG["bot"]["increment"]
                warnings.append("递增数值为 0，已自动修正为默认值 10 秒")
            warnings.extend(w)

            if init_wait >= max_wait:
                max_wait = init_wait + 1
                warnings.append(f"初始回复间隔({init_wait}s)大于等于最大回复间隔，已将最大回复间隔自动修正为 {max_wait}s")

            if init_wait < SAFE_THRESHOLDS["init_wait_time"] or \
               max_wait < SAFE_THRESHOLDS["max_wait_time"] or \
               increment < SAFE_THRESHOLDS["increment"]:
                risk_warning = True

            cfg["bot"]["init_wait_time"] = init_wait
            cfg["bot"]["max_wait_time"] = max_wait
            cfg["bot"]["increment"] = increment

            if "mode" in bot_config:
                mode = bot_config["mode"]
                if mode in ("white_list", "frequency"):
                    cfg["bot"]["mode"] = mode
                else:
                    warnings.append(f"无效的访问控制模式: {mode}，已忽略")

            # 字符串字段统一 strip
            for key in ("admin_id", "test_steam_id", "standby_slot"):
                if key in bot_config:
                    cfg["bot"][key] = str(bot_config[key]).strip()

            # 白名单校验：必须为列表，元素逐个 int 化，失败丢弃
            if "white_list" in bot_config:
                wl = bot_config["white_list"]
                if isinstance(wl, list):
                    cleaned = []
                    for item in wl:
                        try:
                            cleaned.append(int(item))
                        except (ValueError, TypeError):
                            warnings.append(f"白名单元素 {item!r} 无效，已丢弃")
                    cfg["bot"]["white_list"] = cleaned
                else:
                    warnings.append("white_list 必须是列表，已忽略")

            if "frequency" in bot_config:
                freq, w = self._validate_non_negative_int(bot_config["frequency"], "frequency", DEFAULT_CONFIG["bot"]["frequency"])
                if freq == 0:
                    freq = 1
                    w.append("每小时调用次数下限为 1，已自动修正为 1")
                cfg["bot"]["frequency"] = freq
                warnings.extend(w)

            if "max_messages_per_round" in bot_config:
                max_msgs, w = self._validate_non_negative_int(bot_config["max_messages_per_round"], "max_messages_per_round", DEFAULT_CONFIG["bot"]["max_messages_per_round"])
                if max_msgs == 0:
                    max_msgs = 1
                    w.append("每轮最大消息数不能为 0，已自动修正为 1")
                cfg["bot"]["max_messages_per_round"] = max_msgs
                warnings.extend(w)

            if "parallel_count" in bot_config:
                cnt, _ = self._validate_non_negative_int(bot_config["parallel_count"], "parallel_count", 5)
                if cnt < 1:
                    cnt = 1
                if cnt > 20:
                    cnt = 20
                cfg["bot"]["parallel_count"] = cnt

            # 布尔字段统一 bool 化
            for key in ("parallel", "multi_account", "standby_mode", "auto_disable_alt_on_risk", "auto_like"):
                if key in bot_config:
                    cfg["bot"][key] = bool(bot_config[key])

            if not dry_run:
                self.save()
                self._notify_changed()

            return {
                "ok": True,
                "warnings": warnings,
                "risk_warning": risk_warning,
            }

    def set_llm_config(self, llm_config: dict):
        """设置 LLM 配置。"""
        with self._lock:
            if "vendor" in llm_config:
                self._config["llm"]["vendor"] = llm_config["vendor"]
            if "api_key" in llm_config:
                self._config["llm"]["api_key"] = llm_config["api_key"]
                vendor = self._config["llm"].get("vendor", "")
                if vendor and llm_config["api_key"]:
                    self._config["llm"]["vendor_keys"][vendor] = llm_config["api_key"]
            if "base_url" in llm_config:
                self._config["llm"]["base_url"] = llm_config["base_url"]
            if "api_path" in llm_config:
                self._config["llm"]["api_path"] = llm_config["api_path"]
            if "model" in llm_config:
                self._config["llm"]["model"] = llm_config["model"]
            if "max_tokens" in llm_config:
                try:
                    self._config["llm"]["max_tokens"] = int(llm_config["max_tokens"])
                except (ValueError, TypeError):
                    self._config["llm"]["max_tokens"] = 5000
            if "web_search" in llm_config:
                self._config["llm"]["web_search"] = bool(llm_config["web_search"])
            if "show_reasoning" in llm_config:
                self._config["llm"]["show_reasoning"] = bool(llm_config["show_reasoning"])
            self.save()
            self._notify_changed()

    def get_steam_prompt(self) -> str:
        """获取 Steam 评价提示词。"""
        with self._lock:
            content = self._config.get("steam_prompt", {}).get("content", "")
            if not content:
                return STEAM_ANALYST_PROMPT
            return content

    def set_steam_prompt(self, content: str):
        """设置 Steam 评价提示词。"""
        with self._lock:
            self._config["steam_prompt"]["content"] = content
            self.save()
            self._notify_changed()

    def reset_steam_prompt(self):
        """恢复默认 Steam 评价提示词。"""
        with self._lock:
            self._config["steam_prompt"]["content"] = ""
            self.save()
            self._notify_changed()

    def get_steam_recommend_prompt(self) -> str:
        """获取 Steam 推荐提示词。"""
        with self._lock:
            content = self._config.get("steam_recommend_prompt", {}).get("content", "")
            if not content:
                return _get_default_steam_recommend_prompt()
            return content

    def set_steam_recommend_prompt(self, content: str):
        """设置 Steam 推荐提示词。"""
        with self._lock:
            self._config["steam_recommend_prompt"]["content"] = content
            self.save()
            self._notify_changed()

    def reset_steam_recommend_prompt(self):
        """恢复默认 Steam 推荐提示词。"""
        with self._lock:
            self._config["steam_recommend_prompt"]["content"] = ""
            self.save()
            self._notify_changed()

    def set_prompt(self, content: str):
        """设置提示词。"""
        with self._lock:
            self._config["prompt"]["content"] = content
            self.save()
            self._notify_changed()

    def reset_prompt(self):
        """恢复默认提示词。"""
        with self._lock:
            self._config["prompt"]["content"] = ""
            self.save()
            self._notify_changed()

    def reset_all(self):
        """重置所有配置为默认值。"""
        with self._lock:
            self._config = self._deep_copy(DEFAULT_CONFIG)
            self.save()
            self._notify_changed()

    def get_providers(self) -> dict:
        """获取所有 AI 提供商预设。"""
        return dict(AI_PROVIDERS)

    @staticmethod
    def _validate_non_negative_int(value, name: str, default: int) -> tuple:
        """验证非负整数，返回 (修正后的值, 警告列表)。"""
        warnings = []
        if isinstance(value, bool):
            # bool 是 int 子类，显式拒绝避免 True/False 被当作 1/0
            warnings.append(f"{name} 输入无效，已自动修正为默认值 {default}")
            return default, warnings
        try:
            val = int(value)
        except (ValueError, TypeError):
            val = default
            warnings.append(f"{name} 输入无效，已自动修正为默认值 {default}")
            return val, warnings

        if val < 0:
            val = default
            warnings.append(f"{name} 不能为负数，已自动修正为默认值 {default}")
            return val, warnings

        if isinstance(value, float) and value != int(value):
            val = default
            warnings.append(f"{name} 不能为小数，已自动修正为默认值 {default}")

        return val, warnings


# 全局实例
_config_instance = None


def get_config() -> ConfigManager:
    global _config_instance
    if _config_instance is None:
        _config_instance = ConfigManager()
    return _config_instance