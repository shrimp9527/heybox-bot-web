"""
LLM 客户端
统一 OpenAI 兼容 API 调用，支持多厂商自动适配
"""

import base64
import json
import re
import time

import requests

from config.config_manager import get_config
from logger.log_manager import get_logger
from .stats import get_stats


class LLMClient:
    """LLM 客户端单例"""

    _instance = None

    def __new__(cls):
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

    def get_base_url(self) -> str:
        """获取当前 Base URL。"""
        llm_config = self._config.get_llm_config()
        return llm_config.get("base_url", "").rstrip("/")

    def get_api_path(self) -> str:
        """获取 API 路径。"""
        llm_config = self._config.get_llm_config()
        api_path = llm_config.get("api_path", "/chat/completions")
        if api_path and not api_path.startswith("/"):
            api_path = "/" + api_path
        return api_path

    def get_model(self) -> str:
        """获取当前使用的模型名称。"""
        llm_config = self._config.get_llm_config()
        return llm_config.get("model", "")

    def get_api_key(self) -> str:
        """获取 API Key。"""
        llm_config = self._config.get_llm_config()
        return llm_config.get("api_key", "")

    def _build_url(self, api_path: str) -> str:
        """构建完整 API URL。"""
        base_url = self.get_base_url()
        if not api_path.startswith("/"):
            api_path = "/" + api_path
        return base_url + api_path

    def fetch_models(self) -> list:
        """
        获取模型列表。
        调用 /models 端点获取模型列表（使用当前配置）。
        """
        return self.fetch_models_for(self.get_api_key(), self.get_base_url())

    def fetch_models_for(self, api_key: str, base_url: str) -> list:
        """
        使用指定凭据获取模型列表（核心逻辑，供 fetch_models 与 Web 代理端点复用）。
        """
        if not api_key:
            raise Exception("请先填写 API Key")

        base_url = (base_url or "").rstrip("/")
        if not base_url:
            raise Exception("请先配置 Base URL")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        models_url = base_url + "/models"

        try:
            resp = requests.get(models_url, headers=headers, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                models = data.get("data", data.get("models", []))
                model_ids = []
                for m in models:
                    if isinstance(m, dict):
                        mid = m.get("id", m.get("model", ""))
                    elif isinstance(m, str):
                        mid = m
                    else:
                        continue
                    if mid:
                        model_ids.append(mid)
                # 按字母排序
                model_ids.sort()
                self._logger.info(f"获取模型列表成功: 共 {len(model_ids)} 个模型")
                return model_ids
            else:
                error_msg = f"获取模型列表失败: HTTP {resp.status_code}"
                try:
                    error_data = resp.json()
                    if "error" in error_data:
                        error_msg += f" - {error_data['error']}"
                except Exception:
                    error_msg += f" - {resp.text[:200]}"
                self._logger.error(error_msg)
                raise Exception(error_msg)
        except requests.RequestException as e:
            self._logger.error(f"获取模型列表网络错误: {e}")
            raise Exception(f"网络错误: {e}")

    def test_for(self, api_key: str, base_url: str, model: str) -> dict:
        """
        使用指定凭据测试模型连通性（供 Web 代理端点）。
        返回 {"ok": True, "response": ..., "model": model} 或 {"ok": False, "error": ...}
        """
        if not api_key or not base_url or not model:
            return {"ok": False, "error": "api_key / base_url / model 均不能为空"}
        try:
            result = self._call_llm(api_key, base_url, model,
                                    "你是一个智能助手",
                                    "测试模型连通性，仅需回复连通性正常",
                                    None, 50, label="回复模型")
            if result:
                self._logger.info(f"LLM 连通性测试成功: 模型 {model[:30]}, 回复: {result[:80]}")
                return {"ok": True, "response": result, "model": model}
            return {"ok": False, "error": "模型返回空内容", "model": model}
        except Exception as e:
            self._logger.error(f"LLM 连通性测试失败: {e}")
            return {"ok": False, "error": str(e)}

    def _try_request(self, full_url: str, headers: dict, payload: dict, timeout: int = 60, api_key: str = None, stream: bool = False) -> requests.Response:
        """发送请求，401 时自动切换认证方式重试。stream=True 时返回流式响应。"""
        resp = requests.post(full_url, headers=headers, json=payload, timeout=timeout, stream=stream)

        # 401 时回退到 x-api-key 认证（部分国产模型厂商使用此方式）
        if resp.status_code == 401 and headers.get("Authorization", "").startswith("Bearer"):
            alt_headers = dict(headers)
            alt_headers.pop("Authorization", None)
            alt_headers["x-api-key"] = api_key or self.get_api_key()
            self._logger.info("Bearer 认证返回 401，尝试使用 x-api-key 认证...")
            resp = requests.post(full_url, headers=alt_headers, json=payload, timeout=timeout, stream=stream)

        return resp

    def chat(self, system_prompt: str, user_content: str, image_urls: list = None, model_label: str = "回复模型",
             api_key: str = None, base_url: str = None, model: str = None, max_tokens: int = None,
             api_path: str = None) -> str:
        """
        发送对话请求，返回 AI 回复内容。
        支持传入图片 URL 列表，自动构建多模态请求。
        可通过 api_key/base_url/model/api_path 覆盖默认凭据（用于 Steam 评价等专用模型）。
        """
        is_override = api_key is not None  # 是否明确传入了覆盖凭据（包括空字符串）
        api_key = api_key if api_key is not None else self.get_api_key()
        if not api_key:
            raise Exception("请先配置 API Key")

        base_url = (base_url or self.get_base_url()).rstrip("/")
        if not base_url:
            raise Exception("请先配置 Base URL")

        model = model or self.get_model()
        if not model:
            raise Exception("请先选择 AI 模型")

        # 覆盖模式默认 /chat/completions，调用方传入 api_path（如 Steam 评价配置）时优先使用
        api_path = api_path or ("/chat/completions" if is_override else self.get_api_path())
        if not api_path.startswith("/"):
            api_path = "/" + api_path
        full_url = base_url + api_path

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        # 构建用户消息内容（支持图片）
        has_images = image_urls and len(image_urls) > 0
        if has_images:
            user_message_content = self._build_vision_content(user_content, image_urls)
        else:
            user_message_content = user_content

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message_content},
            ],
            "temperature": 1,  # 默认 1，兼容仅接受 temperature=1 的模型
            "max_tokens": max_tokens if max_tokens is not None else self._config.get_llm_config().get("max_tokens", 5000),
        }

        start_time = time.time()
        try:
            resp = self._try_request(full_url, headers, payload, timeout=300, api_key=api_key)
            # 自动适配图片：模型不支持 vision 时回退纯文本
            if resp.status_code == 404 and "image" in resp.text.lower():
                if has_images:
                    self._logger.info("模型不支持图片输入，回退到纯文本模式")
                    payload["messages"][1]["content"] = user_content
                    resp = self._try_request(full_url, headers, payload, timeout=300, api_key=api_key)
            # temperature 固定为 1（payload 中已设置），兼容仅接受 temperature=1 的模型，无需重试
            elapsed = time.time() - start_time

            if resp.status_code == 200:
                data = resp.json()
                choices = data.get("choices", [])
                if choices:
                    message = choices[0].get("message", {})
                    content = message.get("content") or ""
                    # 尝试从 delta 获取（流式响应转发场景）
                    if not content and "delta" in choices[0]:
                        content = choices[0]["delta"].get("content") or ""

                    if not content.strip():
                        self._logger.error(
                            f"AI 模型返回空回复，响应摘要: {json.dumps(data, ensure_ascii=False)[:500]}"
                        )
                        return ""

                    # 标准模型（DeepSeek 等）：content 非空，不走上述分支，处理逻辑完全不变
                    usage = data.get("usage", {})
                    total_tokens = usage.get("total_tokens", 0)
                    self._logger.info(
                        f"AI 模型调用成功 (耗时: {elapsed:.2f}s, "
                        f"prompt_tokens: {usage.get('prompt_tokens', '?')}, "
                        f"completion_tokens: {usage.get('completion_tokens', '?')}, "
                        f"total_tokens: {total_tokens or '?'})"
                    )
                    # 记录主模型 Token 消耗
                    if total_tokens:
                        get_stats().record_llm_call(model_label, model, int(total_tokens))
                    return content.strip()
                else:
                    self._logger.error(f"AI 模型返回空 choices: {json.dumps(data, ensure_ascii=False)[:500]}")
                    raise Exception("AI 模型返回空回复")
            else:
                error_msg = f"AI 模型调用失败: HTTP {resp.status_code}"
                try:
                    error_data = resp.json()
                    if "error" in error_data:
                        err = error_data["error"]
                        if isinstance(err, dict):
                            error_msg += f" - {err.get('message', str(err))}"
                        else:
                            error_msg += f" - {err}"
                except Exception:
                    error_msg += f" - {resp.text[:300]}"
                self._logger.error(error_msg)
                raise Exception(error_msg)
        except requests.RequestException as e:
            self._logger.error(f"AI 模型调用网络错误: {e}")
            raise Exception(f"网络错误: {e}")

    def test_connection(self) -> dict:
        """
        测试 LLM 连通性。
        返回 {"ok": True, "response": "..."} 或 {"ok": False, "error": "..."}
        """
        try:
            model = self.get_model()
            reply = self.chat(
                "你是一个智能助手",
                "测试模型连通性，仅需回复连通性正常",
            )
            # 对于推理模型，chat() 可能返回空但 reasoning_content 有内容
            # 此时判定连接成功但标注为空回复
            if not reply:
                return {"ok": True, "response": "(模型仅返回推理过程，请参见日志)", "model": model}
            self._logger.info(f"LLM 连通性测试成功: 模型 {model}, 回复: {reply[:100]}")
            return {"ok": True, "response": reply, "model": model}
        except Exception as e:
            self._logger.error(f"LLM 连通性测试失败: {e}")
            return {"ok": False, "error": str(e)}

    def test_search_connection(self) -> dict:
        """测试搜索关键词模型连通性。返回 {"ok": True/False, ...}"""
        search_config = self._config.get_llm_search_config()
        api_key = search_config.get("api_key", "")
        base_url = search_config.get("base_url", "")
        model = search_config.get("model", "")
        if not api_key or not base_url or not model:
            self._logger.warn("搜索模型配置不完整，无法测试连接")
            return {"ok": False, "error": "搜索模型配置不完整"}
        try:
            result = self._call_llm(api_key, base_url, model,
                                    "你是一个智能助手",
                                    "测试模型连通性，仅需回复连通性正常",
                                    None, 50, label="搜索模型",
                                    api_path=search_config.get("api_path", ""))
            if result:
                self._logger.info(f"搜索模型连通性测试成功: 模型 {model[:30]}, 回复: {result[:80]}")
                return {"ok": True, "response": result, "model": model}
            return {"ok": True, "response": "(空)", "model": model}
        except Exception as e:
            self._logger.error(f"搜索模型连通性测试失败: {e}")
            return {"ok": False, "error": str(e)}

    def test_judge_connection(self) -> dict:
        """测试判断模型连通性。返回 {"ok": True/False, ...}"""
        judge_config = self._config.get_llm_search_judge_config()
        api_key = judge_config.get("api_key", "")
        base_url = judge_config.get("base_url", "")
        model = judge_config.get("model", "")
        if not api_key or not base_url or not model:
            self._logger.warn("判断模型配置不完整，无法测试连接")
            return {"ok": False, "error": "判断模型配置不完整"}
        try:
            result = self._call_llm(api_key, base_url, model,
                                    "你是一个智能助手",
                                    "测试模型连通性，仅需回复连通性正常",
                                    None, 50, label="判断模型")
            if result:
                self._logger.info(f"判断模型连通性测试成功: 模型 {model[:30]}, 回复: {result[:80]}")
                return {"ok": True, "response": result, "model": model}
            return {"ok": True, "response": "(空)", "model": model}
        except Exception as e:
            self._logger.error(f"判断模型连通性测试失败: {e}")
            return {"ok": False, "error": str(e)}

    def _download_image_as_data_uri(self, url: str) -> str:
        """
        下载图片，缩小尺寸并转换为 base64 data URI。
        避免大图片导致请求超时。
        """
        try:
            resp = requests.get(url, timeout=15, stream=True)
            resp.raise_for_status()
            content_type = resp.headers.get("Content-Type", "image/jpeg")
            if not content_type.startswith("image/"):
                raise ValueError(f"非图片 Content-Type: {content_type}")
            # 下载上限 10MB：先预检 Content-Length，再分块累计超限则放弃
            max_size = 10 * 1024 * 1024
            content_length = resp.headers.get("Content-Length")
            if content_length and int(content_length) > max_size:
                raise ValueError(f"图片超过 10MB 上限（Content-Length={content_length}）")
            chunks = []
            downloaded = 0
            for chunk in resp.iter_content(chunk_size=65536):
                downloaded += len(chunk)
                if downloaded > max_size:
                    raise ValueError("图片超过 10MB 上限，放弃下载")
                chunks.append(chunk)
            raw = b"".join(chunks)

            # 尝试用 Pillow 缩小图片，减少 payload
            try:
                from io import BytesIO
                from PIL import Image
                img = Image.open(BytesIO(raw))
                if max(img.size) > 768:
                    ratio = 768.0 / max(img.size)
                    new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
                    img = img.resize(new_size, Image.LANCZOS)
                buf = BytesIO()
                # 统一转换为 RGB 再保存为 JPEG（处理 GIF/PNG 调色板模式）
                if img.mode in ('P', 'RGBA', 'LA', 'L'):
                    img = img.convert('RGB')
                img.save(buf, format='JPEG', quality=85)
                raw = buf.getvalue()
                content_type = "image/jpeg"  # 统一输出 JPEG
            except ImportError:
                pass  # 无 Pillow，使用原图

            b64 = base64.b64encode(raw).decode("ascii")
            return f"data:{content_type};base64,{b64}"
        except Exception as e:
            self._logger.warn(f"下载/处理图片失败，回退到原始 URL: {url}, 错误: {e}")
            return url  # 回退到原始 URL

    def _build_vision_content(self, text: str, image_urls: list) -> list:
        """
        构建多模态 vision 请求的消息内容。
        将 HTTP URL 转换为 base64 data URI，兼容不支持直接 URL 的模型。
        """
        content = [{"type": "text", "text": text}]
        for url in image_urls:
            if not url:
                continue
            data_uri = self._download_image_as_data_uri(url)
            content.append({
                "type": "image_url",
                "image_url": {"url": data_uri},
            })
        return content

    def generate_search_query(self, context: str, image_urls: list = None) -> str:
        """
        分析帖子内容和用户消息，生成适合搜索引擎的查询关键词。
        使用独立的 llm_search 配置，未填写时回退到主 LLM 配置。
        支持图片识别以生成更精准的搜索词。
        返回纯关键词文本，失败时返回空字符串。
        """
        prompt = (
            "你是一个搜索关键词提取助手。请分析以下帖子和用户消息，提取一个适合搜索引擎查询的关键词短语（不超过20字）。"
            "直接输出关键词，不要加任何解释、引号或修饰。"
        )

        # 使用搜索专用模型配置
        search_config = self._config.get_llm_search_config()
        api_key = search_config.get("api_key", "")
        base_url = search_config.get("base_url", "")
        model = search_config.get("model", "")

        if not api_key or not base_url or not model:
            self._logger.warn("搜索模型配置不完整，跳过关键词生成")
            return ""

        try:
            # 使用统一的 LLM 调用（自动处理图片、认证、404 回退）
            max_tokens = search_config.get("max_tokens", 5000)
            result = self._call_llm(api_key, base_url, model, prompt, context, image_urls, max_tokens,
                                    label="搜索模型", api_path=search_config.get("api_path", ""))
            if result:
                result = result.strip().strip('"').strip("'").strip()
                self._logger.info(f"搜索关键词生成成功 (模型={model[:30]})")
                return result
            self._logger.warn("搜索关键词生成失败")
        except Exception as e:
            self._logger.warn(f"搜索关键词生成异常: {e}")

        return ""

    def _call_llm(self, api_key: str, base_url: str, model: str,
                   system_prompt: str, user_content: str,
                   image_urls: list = None, max_tokens: int = 200, label: str = "搜索模型",
                   api_path: str = None) -> str:
        """
        统一的 LLM 调用（用于搜索等辅助任务）。
        自动处理图片、401 认证回退、404 图片回退。
        label 用于 Token 统计的模型类型标签（"搜索模型"/"判断模型"等）。
        api_path 非空时使用该路径（规范化前导斜杠），否则默认 /chat/completions。
        """
        api_path = api_path or "/chat/completions"
        if not api_path.startswith("/"):
            api_path = "/" + api_path
        full_url = base_url.rstrip("/") + api_path

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        has_images = image_urls and len(image_urls) > 0
        if has_images:
            self._logger.info(f"搜索模型收到 {len(image_urls)} 张图片，生成多模态请求")
        user_msg = self._build_vision_content(user_content, image_urls) if has_images else user_content

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            "temperature": 1,
            "max_tokens": max_tokens,
        }

        resp = self._try_request(full_url, headers, payload, timeout=120, api_key=api_key)

        # 图片回退：某些模型 (DeepSeek 等) 返回 400，某些返回 404
        if has_images and resp.status_code in (400, 404) and ("image" in resp.text.lower() or "image_url" in resp.text.lower()):
            self._logger.info("搜索模型不支持图片，回退纯文本重试")
            payload["messages"][1]["content"] = user_content
            resp = self._try_request(full_url, headers, payload, timeout=120, api_key=api_key)

        if resp.status_code == 200:
            data = resp.json()
            usage = data.get("usage", {})
            total_tokens = usage.get("total_tokens", 0)
            choices = data.get("choices", [])
            if choices:
                message = choices[0].get("message", {})
                result = message.get("content", "") or ""
                if not result:
                    result = message.get("reasoning_content", "") or ""
                    if result:
                        self._logger.info("搜索模型使用 reasoning_content")
                if not result:
                    self._logger.warn("搜索模型返回空内容，完整响应: " + json.dumps(data, ensure_ascii=False)[:300])
                    return ""
                # 按调用方传入的标签统计 Token 消耗
                if total_tokens:
                    get_stats().record_llm_call(label, model, int(total_tokens))
                return result.strip()
            self._logger.warn("搜索模型返回空 choices")
        else:
            err_text = resp.text[:200] if resp.text else "(empty)"
            self._logger.error(f"搜索模型 HTTP {resp.status_code}: {err_text}")

        return ""

    def judge_search_needed(self, context: str) -> bool:
        """
        使用独立的判断模型分析用户消息，判断是否需要联网搜索。
        返回 True 表示需要搜索，False 表示不需要。
        配置不完整时返回 True（默认执行搜索）。
        模型判断为"否"时，追加关键字启发式规则兜底，避免小模型误判。
        """
        judge_config = self._config.get_llm_search_judge_config()
        if not judge_config.get("enabled"):
            return True
        api_key = judge_config.get("api_key", "")
        base_url = judge_config.get("base_url", "")
        model = judge_config.get("model", "")
        max_tokens = judge_config.get("max_tokens", 200)

        if not api_key or not base_url or not model:
            self._logger.info("搜索需求判断模型未完整配置，默认执行搜索")
            return True

        prompt = (
            "请判断以下用户消息是否需要通过联网搜索获取最新信息。"
            "仅回答 '是' 或 '否'，不要输出其他内容。\n"
            "需要搜索的情况：\n"
            "  - 询问商品/游戏/硬件的发售日期、价格、配置要求、评测\n"
            "  - 询问最新版本更新内容、补丁说明、DLC信息\n"
            "  - 当前正在发生的事件、新闻、官方公告\n"
            "  - 实时数据（天气、汇率、股价、销量排名）\n"
            "  - 具体厂商或产品的规格参数、上市信息\n"
            "不需要搜索的情况：\n"
            "  - 纯粹情感表达（如'好开心''太难了''牛啊'）\n"
            "  - 观点讨论、主观评价、经验分享\n"
            "  - 常识性问题（如'地球是圆的'）\n"
            "  - 闲聊、打招呼、无实质内容的@"
        )

        need_search = True  # 默认执行搜索

        try:
            result = self._call_llm(api_key, base_url, model, prompt, context, None, max_tokens, label="判断模型")
            if result:
                raw = result.strip()
                result_lower = raw.lower()
                # 精确判定：避免"不是"中的"是"被误匹配；"no" 用词边界正则防止误匹配子串
                is_no = "否" in result_lower or re.search(r'\bno\b', result_lower) or "不是" in result_lower or "不需要" in result_lower
                is_yes = "是" in result_lower or "yes" in result_lower or "需要" in result_lower
                need_search = is_yes and not is_no
                self._logger.info(f"搜索需求判断: {'需要搜索' if need_search else '不需要搜索'} (模型={model[:30]}, 回复={raw[:80]})")
            else:
                self._logger.info("搜索需求判断模型返回空内容，默认执行搜索")
        except Exception as e:
            self._logger.warn(f"搜索需求判断异常: {e}，默认执行搜索")

        # 关键字启发式兜底：模型判"否"但消息包含明显的时效性关键词时，强制覆盖
        if not need_search:
            time_sensitive_keywords = [
                # 时间/时效
                "什么时候", "何时", "哪天", "几点", "多久", "还要多久",
                "发售", "发布", "上线", "推出", "出了吗", "出了没",
                "最新", "新出", "刚出", "最近", "现在", "当前", "今天", "今年",
                # 版本/更新
                "更新", "新版本", "补丁", "dlc", "DLC", "续作", "重制", "重制版",
                # 价格/产品
                "价格", "多少钱", "售价", "降价", "涨价", "值得买", "性价比",
                "配置要求", "配置", "参数", "规格",
                # 实时数据
                "天气", "汇率", "股价", "排名", "销量", "在线人数",
                # 评测/资讯
                "评测", "测评", "评分", "口碑", "预告", "预告片", "官方",
                # 活动/赛事
                "活动", "赛事", "比赛", "促销", "打折", "限免", "会免",
            ]
            context_lower = context.lower()
            for kw in time_sensitive_keywords:
                if kw.lower() in context_lower:
                    self._logger.info(f"关键字兜底触发（'{kw}'），强制执行联网搜索")
                    need_search = True
                    break

        return need_search

    def chat_stream(self, system_prompt: str, user_content: str, on_token: callable = None, image_urls: list = None,
                    api_key: str = None, base_url: str = None, model: str = None, max_tokens: int = None,
                    api_path: str = None) -> str:
        """
        流式对话请求。通过 on_token(content, is_reasoning) 回调实时推送增量内容。
        支持传入图片 URL 列表。
        返回完整的最终回复文本。
        """
        is_override = api_key is not None  # 是否明确传入了覆盖凭据（需在覆盖前保存）
        api_key = api_key if api_key is not None else self.get_api_key()
        if not api_key:
            raise Exception("请先配置 API Key")
        base_url = (base_url or self.get_base_url()).rstrip("/")
        if not base_url:
            raise Exception("请先配置 Base URL")
        model = model or self.get_model()
        if not model:
            raise Exception("请先选择 AI 模型")
        # 覆盖模式默认 /chat/completions，调用方传入 api_path（如 Steam 评价配置）时优先使用
        api_path = api_path or ("/chat/completions" if is_override else self.get_api_path())
        full_url = base_url + api_path if api_path.startswith("/") else base_url + "/" + api_path

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        # 构建用户消息内容（支持图片）
        has_images = image_urls and len(image_urls) > 0
        user_msg_content = self._build_vision_content(user_content, image_urls) if has_images else user_content

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg_content},
            ],
            "temperature": 1,
            "max_tokens": max_tokens if max_tokens is not None else self._config.get_llm_config().get("max_tokens", 5000),
            "stream": True,
        }

        try:
            resp = self._try_request(full_url, headers, payload, timeout=300, api_key=api_key, stream=True)
            if resp.status_code != 200:
                self._logger.warn(f"流式请求失败 (HTTP {resp.status_code})，回退到普通模式")
                return ""
            full_content = ""
            last_data_time = time.time()  # 上次收到数据的时间，空闲超 180s 中断
            for line in resp.iter_lines(decode_unicode=False):
                if time.time() - last_data_time > 180:
                    self._logger.warn("流式响应超过 180s 未收到数据，已中断")
                    resp.close()
                    break
                if not line:
                    continue
                last_data_time = time.time()
                # 尝试 UTF-8 解码，失败则跳过损坏的行
                try:
                    line_str = line.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                line_str = line_str.strip()
                if not line_str or not line_str.startswith("data:"):
                    continue
                data_str = line_str[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk_json = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                choices = chunk_json.get("choices", [])
                if not choices:
                    continue
                delta = choices[0].get("delta", {})
                reasoning = delta.get("reasoning_content", "")
                content = delta.get("content", "")
                if reasoning and on_token:
                    on_token(reasoning, True)
                if content:
                    full_content += content
                    if on_token:
                        on_token(content, False)
            return full_content.strip()
        except requests.RequestException as e:
            self._logger.error(f"流式请求网络错误: {e}")
            return ""


# 全局实例
_llm_instance = None


def get_llm_client() -> LLMClient:
    global _llm_instance
    if _llm_instance is None:
        _llm_instance = LLMClient()
    return _llm_instance