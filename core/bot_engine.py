"""
机器人引擎
核心消息轮询、间隔控制、回复调度
"""

import random
import re
import time
import threading

from config.config_manager import get_config
from logger.log_manager import get_logger
from . import heybox_api
from .access_control import get_access_control
from .llm_client import get_llm_client
from .session_manager import get_session
from .stats import get_stats
from .web_search import search_web, SEARCH_PREFIX


class BotEngine:
    """机器人引擎单例"""

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
        self._session = get_session()
        self._llm = get_llm_client()
        self._access = get_access_control()

        self._running = False
        self._stop_event = threading.Event()
        self._thread = None
        # start/stop 控制锁，防止并发启停竞态
        self._control_lock = threading.Lock()
        # 首次拉取标记（用于按服务器时间校准基准）
        self._first_fetch = False

        # 间隔控制状态
        self._current_interval = 0
        self._last_at_timestamp = 0.0
        # 实时倒计时
        self._wait_start_time = 0.0
        self._wait_duration = 0

        # 已处理消息 ID 集合
        self._processed_messages = set()
        # 延后处理的消息（本轮超出上限，下一轮优先处理）
        self._deferred_messages = []
        # 处理失败待重试的消息（仅重试一次，下轮并入延后队列）
        self._retry_messages = []
        # 并行处理时的线程安全锁（同时保护 _account_cycle）
        self._msg_lock = threading.Lock()
        # 多账号轮转计数器
        self._account_cycle = 0
        # 副号失败冷却状态 {slot: [连续失败次数, 冷却截止时间戳]}
        self._slot_fail = {}
        self._slot_fail_lock = threading.Lock()
        # AI 回复线程池（后台异步回复，不阻塞消息检测）
        # 在 start() 中创建，stop() 后重新创建，避免线程池关闭后无法复用
        self._reply_executor = None

        # 状态回调
        self._status_callback = None

    @property
    def is_running(self) -> bool:
        return self._running

    def set_status_callback(self, callback):
        """设置状态更新回调。"""
        self._status_callback = callback

    def _update_status(self, status: str, details: dict = None):
        """更新运行状态。"""
        if self._status_callback:
            try:
                self._status_callback(status, details or {})
            except Exception:
                pass

    def start(self):
        """启动机器人。"""
        with self._control_lock:
            if self._running:
                if self._thread and not self._thread.is_alive():
                    self._logger.warn("检测到机器人线程已异常退出，自动重置状态")
                    self._running = False
                    self._thread = None
                else:
                    self._logger.warn("机器人已在运行中")
                    return

            if not self._session.is_logged_in:
                self._logger.error("机器人启动失败: 未登录")
                raise Exception("请先登录小黑盒账号")

            self._last_at_timestamp = time.time()
            self._first_fetch = True  # 首次拉取按服务器时间校准基准
            self._processed_messages.clear()
            self._deferred_messages.clear()
            self._retry_messages.clear()

            # 创建/重建后台回复线程池
            from concurrent.futures import ThreadPoolExecutor
            if self._reply_executor is not None:
                # 不等待任务结束，并尽力取消未开始的任务（旧解释器无 cancel_futures 时降级）
                try:
                    self._reply_executor.shutdown(wait=False, cancel_futures=True)
                except TypeError:
                    self._reply_executor.shutdown(wait=False)
            self._reply_executor = ThreadPoolExecutor(max_workers=20, thread_name_prefix="reply")

            self._running = True
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run_loop, daemon=True)
            self._thread.start()
            self._logger.info("机器人已启动，只处理启动后的 @ 消息")
            self._update_status("running", {"message": "机器人运行中"})

    def stop(self):
        """停止机器人。"""
        with self._control_lock:
            if not self._running:
                return

            self._running = False
            self._stop_event.set()
            # 关闭后台回复线程池：不等待任务结束，并尽力取消未开始的任务
            # （cancel_futures 在部分旧解释器上不可用，降级为普通 shutdown）
            if getattr(self, '_reply_executor', None) is not None:
                try:
                    self._reply_executor.shutdown(wait=False, cancel_futures=True)
                except TypeError:
                    self._reply_executor.shutdown(wait=False)
                self._reply_executor = None
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=5)
            self._logger.info("机器人已停止")
            self._update_status("stopped", {"message": "机器人已停止"})

    def _run_loop(self):
        """机器人主循环。"""
        self._current_interval = 0
        is_first_run = True
        _params_logged = False

        while self._running and self._thread is threading.current_thread() and not self._stop_event.is_set():
            # 每轮重新读取配置实现热更新
            bot_config = self._config.get_bot_config()
            init_wait = bot_config.get("init_wait_time", 10)
            max_wait = bot_config.get("max_wait_time", 60)
            increment = bot_config.get("increment", 10)

            if not _params_logged:
                self._logger.info(f"机器人间隔参数: 初始={init_wait}s, 最大={max_wait}s, 递增={increment}s")
                _params_logged = True
            try:
                if is_first_run:
                    self._logger.info("开始第一次消息检测")
                    is_first_run = False

                check_start = time.time()
                self._logger.info(f"开始消息检测 (当前间隔: {self._current_interval}s)")
                self._update_status("checking", {"interval": self._current_interval})

                self._check_messages()

                check_elapsed = time.time() - check_start
                self._logger.info(f"消息检测完成，耗时: {check_elapsed:.2f}s")

                next_interval = self._current_interval + increment

                if next_interval >= max_wait:
                    wait_time = max_wait
                    self._current_interval = init_wait
                    self._logger.info(
                        f"间隔已达上限: 本次等待 {max_wait}s，下次重置为 {init_wait}s"
                    )
                else:
                    wait_time = next_interval
                    self._current_interval = next_interval

                self._update_status("waiting", {
                    "interval": wait_time,
                    "next_interval": self._current_interval,
                })

                self._wait_start_time = time.time()
                self._wait_duration = wait_time
                self._logger.info(f"等待 {wait_time}s 后进行下一次检测")
                self._stop_event.wait(wait_time)
                self._wait_duration = 0

            except Exception as e:
                self._logger.error(f"机器人运行异常: {e}")
                self._update_status("error", {"error": str(e)})
                self._stop_event.wait(5)

    def _process_batch_parallel(self, tasks: list, batch_label: str) -> int:
        """并行处理一批消息，返回已受理计数。
        注意：已处理标记不在这里打，只由 _generate_and_publish_reply 成功回调
        与"测试 响应"路径负责，避免受理但未回复成功的消息被误判为已处理。"""
        if not tasks:
            return 0
        from concurrent.futures import ThreadPoolExecutor, as_completed

        max_workers = min(len(tasks), max(1, self._config.get_bot_config().get("parallel_count", 5)))
        count = 0
        errors = []

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {}
            for i, msg in enumerate(tasks, 1):
                tag = f"{batch_label}#{i}"
                futures[executor.submit(self._process_message, msg, tag)] = msg

            for future in as_completed(futures):
                if self._stop_event.is_set():
                    try:
                        executor.shutdown(wait=False, cancel_futures=True)
                    except TypeError:
                        executor.shutdown(wait=False)
                    break
                msg = futures[future]
                msg_id = msg.get("message_id", 0)
                try:
                    future.result()
                    count += 1
                except Exception as e:
                    errors.append(f"消息 {msg_id}: {e}")
                    self._schedule_retry(msg)

        if errors:
            for err in errors:
                self._logger.error(f"[{batch_label}] 处理异常: {err}")

        return count

    def _check_messages(self):
        """检测并处理 @ 消息。配额对半分配，支持并行模式。"""
        try:
            bot_config = self._config.get_bot_config()
            max_per_round = bot_config.get("max_messages_per_round", 3)
            parallel_mode = bot_config.get("parallel", False)

            # 清理已处理消息缓存防止内存无限增长（超过1000条时全量清理）
            if len(self._processed_messages) > 1000:
                self._logger.info(f"清理已处理消息缓存（当前 {len(self._processed_messages)} 条）")
                self._processed_messages.clear()

            new_quota = (max_per_round + 1) // 2
            deferred_quota = max_per_round - new_quota

            self._deferred_messages = [
                m for m in self._deferred_messages
                if m.get("message_id", 0) not in self._processed_messages
            ]

            # 上一轮处理失败待重试的消息并入延后队列头部，本轮优先处理（仅重试一次）
            if self._retry_messages:
                with self._msg_lock:
                    retries, self._retry_messages = self._retry_messages, []
                retries = [m for m in retries
                           if m.get("message_id", 0) not in self._processed_messages]
                self._deferred_messages = retries + self._deferred_messages

            unread_messages = self._fetch_unread_messages()
            pending = [m for m in (unread_messages or [])
                       if m.get("message_id", 0) not in self._processed_messages]

            # 为每条新发现的 @ 消息建立任务面板任务（延后的在面板中显示为排队等待）
            for m in pending:
                self._ensure_task(m)

            new_batch = pending[:new_quota]
            overflow = pending[new_quota:]

            total_processed = 0

            if parallel_mode:
                thread_cnt = min(max(len(new_batch), len(self._deferred_messages)), max(1, bot_config.get("parallel_count", 5)))
                self._logger.info(f"🚀 本轮并行处理 {len(new_batch)}条新消息 + {min(len(self._deferred_messages), deferred_quota)}条延后消息 ({thread_cnt}线程)")

                new_count = self._process_batch_parallel(new_batch, "新")
                total_processed += new_count

                for msg in overflow:
                    if msg.get("message_id", 0) not in self._processed_messages:
                        self._deferred_messages.append(msg)
                self._trim_deferred_queue()

                deferred_tasks = self._deferred_messages[:deferred_quota]
                self._deferred_messages = self._deferred_messages[deferred_quota:]
                deferred_count = self._process_batch_parallel(deferred_tasks, "延后")
                total_processed += deferred_count
            else:
                new_count = 0
                for i, msg in enumerate(new_batch, 1):
                    if self._stop_event.is_set():
                        return
                    tag = f"新#{i}"
                    try:
                        self._process_message(msg, tag)
                        new_count += 1
                    except Exception as e:
                        self._logger.error(f"[{tag}] 处理异常: {e}")
                        self._schedule_retry(msg, tag)
                    time.sleep(1)
                total_processed += new_count

                for msg in overflow:
                    if msg.get("message_id", 0) not in self._processed_messages:
                        self._deferred_messages.append(msg)
                self._trim_deferred_queue()

                deferred_count = 0
                while self._deferred_messages and deferred_count < deferred_quota:
                    if self._stop_event.is_set():
                        return
                    msg = self._deferred_messages.pop(0)
                    deferred_count += 1
                    tag = f"延后#{deferred_count}"
                    try:
                        self._process_message(msg, tag)
                    except Exception as e:
                        self._logger.error(f"[{tag}] 处理异常: {e}")
                        self._schedule_retry(msg, tag)
                    time.sleep(1)
                total_processed += deferred_count

            mode_tag = "并行" if parallel_mode else "串行"
            if total_processed > 0:
                self._logger.info(
                    f"[{mode_tag}] 本轮处理 {total_processed} 条，"
                    f"延后队列剩余 {len(self._deferred_messages)} 条"
                )
            else:
                self._logger.info("没有未读 @ 消息")

        except Exception as e:
            self._logger.error(f"消息检测失败: {e}")

    def _trim_deferred_queue(self):
        """延后队列上限 100 条，超出时丢弃最旧的并告警。"""
        if len(self._deferred_messages) > 100:
            dropped = len(self._deferred_messages) - 100
            self._deferred_messages = self._deferred_messages[dropped:]
            self._logger.warn(f"延后队列超出 100 条上限，已丢弃最旧的 {dropped} 条")

    def _fetch_unread_messages(self) -> list:
        """拉取未读 @ 消息。"""
        session = self._session
        if not session.heybox_id or not session.is_logged_in:
            return []

        unread = []
        max_ts = self._last_at_timestamp
        max_server_ts = 0.0  # 首次拉取时见到的最大服务器时间戳
        offset = 0

        while True:
            try:
                messages = heybox_api.get_at_message(session.heybox_id, offset)
            except Exception as e:
                self._logger.error(f"获取 @ 消息失败: {e}")
                break

            if not messages:
                break

            page_unread = 0
            for msg in messages:
                try:
                    ts = float(msg.get("timestamp", "0"))
                except (ValueError, TypeError):
                    continue

                if ts > max_server_ts:
                    max_server_ts = ts
                if ts > max_ts:
                    max_ts = ts
                if ts > self._last_at_timestamp:
                    unread.append(msg)
                    page_unread += 1

            if page_unread != len(messages) or len(messages) < heybox_api.MESSAGE_NUM_LIMIT:
                break

            offset += heybox_api.MESSAGE_NUM_LIMIT

        # 首次拉取：若见到了任何消息，以服务器最大时间戳为基准（避免本地时钟漂移），历史消息不处理
        if self._first_fetch:
            self._first_fetch = False
            if max_server_ts > 0:
                self._last_at_timestamp = max_server_ts
                self._logger.info(f"首次拉取完成，已按服务器时间校准基准 (timestamp={max_server_ts})")
                return []

        self._last_at_timestamp = max_ts
        return unread

    def _submit_reply(self, msg: dict, context_text: str, image_urls: list, search_results: str,
                      user_id: str, system_prompt_override: str = None, append_text: str = "",
                      tag: str = "", task_id: int = None):
        """提交后台回复任务。stop 并发导致线程池已关闭/置 None 时不再抛异常逃出，
        记录日志、标记任务失败并回滚配额，消息进入重试队列。"""
        tag_prefix = f"[{tag}] " if tag else ""
        executor = self._reply_executor
        try:
            if executor is None:
                raise RuntimeError("回复线程池已关闭（机器人已停止）")
            executor.submit(
                self._generate_and_publish_reply,
                msg, context_text, image_urls, search_results, user_id,
                system_prompt_override, append_text, tag, task_id
            )
        except (AttributeError, RuntimeError) as e:
            self._logger.warn(f"{tag_prefix}后台回复任务提交失败: {e}", user_id=user_id)
            from .task_tracker import get_task_tracker
            get_task_tracker().update(task_id, "failed", error=f"任务提交失败: {e}")
            self._access.refund(user_id)  # 失败回滚频率配额
            self._schedule_retry(msg, tag)

    def _schedule_retry(self, msg: dict, tag: str = ""):
        """为处理失败的消息安排一次（仅一次）下轮重试；已重试过的直接丢弃。"""
        if msg.get("_retried"):
            return
        msg["_retried"] = True
        with self._msg_lock:
            self._retry_messages.append(msg)
        tag_prefix = f"[{tag}] " if tag else ""
        self._logger.info(f"{tag_prefix}本条消息将在下一轮检测时重试一次",
                          user_id=msg.get("user", {}).get("userid", ""))

    def _generate_and_publish_reply(self, msg: dict, context_text: str, image_urls: list, search_results: str, user_id: str, system_prompt_override: str = None, append_text: str = "", tag: str = "", task_id: int = None):
        """生成 AI 回复并发布（后台线程调用，故障隔离）。成功发布后才标记消息为已处理。"""
        tag_prefix = f"[{tag}] " if tag else ""
        msg_id = msg.get("message_id", 0)
        try:
            success = self._do_generate_and_publish_reply(
                msg, context_text, image_urls, search_results, user_id,
                system_prompt_override, append_text, tag, tag_prefix, msg_id, task_id
            )
            if success:
                with self._msg_lock:
                    self._processed_messages.add(msg_id)
            else:
                # 回复失败：安排一次下轮重试（时间戳基准已推进，失败消息不会再被拉到）
                self._schedule_retry(msg, tag)
            return success
        except Exception as e:
            import traceback
            self._logger.error(f"{tag_prefix}后台回复异常: {e}\n{traceback.format_exc()}", user_id=user_id)
            from .task_tracker import get_task_tracker
            get_task_tracker().update(task_id, "failed", error=str(e))
            self._access.refund(user_id)  # 失败回滚频率配额
            self._schedule_retry(msg, tag)
            return False

    def _do_generate_and_publish_reply(self, msg, context_text, image_urls, search_results, user_id, system_prompt_override, append_text, tag, tag_prefix, msg_id, task_id=None):
        from .task_tracker import get_task_tracker
        tracker = get_task_tracker()
        tracker.update(task_id, "generating")
        llm_config = self._config.get_llm_config()
        show_reasoning = llm_config.get("show_reasoning", False)
        model_label = "AI评价库存" if system_prompt_override else "回复模型"

        override_api_key = None
        override_base_url = None
        override_model = None
        override_max_tokens = None
        override_api_path = None
        if system_prompt_override:
            steam_llm = self._config.get_llm_steam_config()
            override_api_key = steam_llm.get("api_key", "") or None
            override_base_url = steam_llm.get("base_url", "") or None
            override_model = steam_llm.get("model", "") or None
            override_max_tokens = steam_llm.get("max_tokens")
            override_api_path = steam_llm.get("api_path", "") or None
            if override_api_key:
                self._logger.info(f"{tag_prefix}Steam 评价使用专用模型: {override_model}", user_id=user_id)

        start_time = time.time()
        try:
            if image_urls:
                self._logger.info(f"{tag_prefix}调用 AI (含{len(image_urls)}张图片)", user_id=user_id)
            else:
                self._logger.info(f"{tag_prefix}调用 AI 生成回复", user_id=user_id)
            clean_search = search_results[len(SEARCH_PREFIX):].strip() if search_results and search_results.startswith(SEARCH_PREFIX) else (search_results or "")
            system_prompt = system_prompt_override or self._config.get_prompt()
            if clean_search:
                system_prompt += "\n\n" + clean_search + "\n请结合以上搜索结果和上下文生成回复。"

            if show_reasoning:
                self._logger.info(f"{tag_prefix}启用流式传输", user_id=user_id)
                reasoning_buffer = ""
                def on_stream_token(token, is_reasoning):
                    nonlocal reasoning_buffer
                    if is_reasoning:
                        reasoning_buffer += token

                reply_text = self._llm.chat_stream(
                    system_prompt, context_text, on_stream_token, image_urls,
                    api_key=override_api_key,
                    base_url=override_base_url,
                    model=override_model,
                    max_tokens=override_max_tokens,
                    api_path=override_api_path,
                )
                if reasoning_buffer:
                    self._logger.info(f"{tag_prefix}[REASONING]{reasoning_buffer}", user_id=user_id)
                if not reply_text:
                    self._logger.info(f"{tag_prefix}流式未生成回复，回退普通模式", user_id=user_id)
                    reply_text = self._llm.chat(system_prompt, context_text, image_urls,
                                                model_label=model_label,
                                                api_key=override_api_key,
                                                base_url=override_base_url,
                                                model=override_model,
                                                max_tokens=override_max_tokens,
                                                api_path=override_api_path)
            else:
                reply_text = self._llm.chat(system_prompt, context_text, image_urls,
                                            model_label=model_label,
                                            api_key=override_api_key,
                                            base_url=override_base_url,
                                            model=override_model,
                                            max_tokens=override_max_tokens,
                                            api_path=override_api_path)

            if not reply_text:
                self._logger.error(f"{tag_prefix}AI 返回空回复", user_id=user_id)
                tracker.update(task_id, "failed", error="AI 返回空回复")
                self._access.refund(user_id)  # 失败回滚频率配额
                return False

            elapsed = time.time() - start_time
            self._logger.info(f"{tag_prefix}AI 回复成功 ({elapsed:.1f}s): {reply_text}", user_id=user_id)
        except Exception as e:
            self._logger.error(f"{tag_prefix}AI 回复失败: {e}", user_id=user_id)
            tracker.update(task_id, "failed", error=f"AI 回复失败: {e}")
            get_stats().record_reply_fail()
            self._access.refund(user_id)  # 失败回滚频率配额
            return False

        try:
            tracker.update(task_id, "publishing")
            self._publish_reply(msg, reply_text + append_text, tag=tag, user_id=user_id)
            get_stats().record_reply_success()
            tracker.update(task_id, "replied", reply_text=reply_text + append_text)
            # 如果是 Steam 评价，提取等级用于仪表盘饼图
            if system_prompt_override:
                m = re.search(r'【库存等级】\s*\n?\s*(SSS|SS|S|A|B|C|D)', reply_text)
                if m:
                    get_stats().record_steam_rating(m.group(1))
            return True
        except Exception as e:
            self._logger.error(f"{tag_prefix}发布回复失败: {e}", user_id=user_id)
            tracker.update(task_id, "failed", error=f"发布回复失败: {e}")
            self._access.refund(user_id)  # 失败回滚频率配额
            return False

    def _ensure_task(self, msg: dict) -> int:
        """为 @ 消息创建/获取任务面板任务，task_id 挂在 msg['_task_id'] 上。"""
        if msg.get("_task_id"):
            return msg["_task_id"]
        from .task_tracker import get_task_tracker
        user = msg.get("user", {})
        question = heybox_api.plain_heybox_mention_text(msg.get("text", "")).strip()
        task_id = get_task_tracker().create(
            msg.get("message_id", 0),
            user.get("userid", ""),
            user.get("username", ""),
            user.get("avatar", ""),
            question,
        )
        msg["_task_id"] = task_id
        return task_id

    def _process_message(self, msg: dict, tag: str = ""):
        """处理单条 @ 消息。"""
        user = msg.get("user", {})
        user_id = user.get("userid", "")
        username = user.get("username", "")
        msg_id = msg.get("message_id", 0)
        text = msg.get("text", "")
        tag_prefix = f"[{tag}] " if tag else ""

        # 任务面板：确保任务存在（_check_messages 已创建时直接复用）
        task_id = self._ensure_task(msg)
        from .task_tracker import get_task_tracker
        tracker = get_task_tracker()

        self._logger.info(f"{tag_prefix}用户「{username}」开始处理", user_id=user_id)

        from .steam_games import account_id_to_steam64

        # 管理员测试命令（admin 不受白名单/频率限制，优先于 access 检查）
        bot_config = self._config.get_bot_config()
        admin_id = bot_config.get("admin_id", "")
        if admin_id and str(user_id) == str(admin_id):
            plain = heybox_api.plain_heybox_mention_text(text).strip()
            if plain == "测试 评价":
                self._logger.info(f"[测试] 管理员触发库存评价测试", user_id=user_id)
                from .steam_games import fetch_games, _format_games_text
                test_id_str = bot_config.get("test_steam_id", "").strip()
                if not test_id_str:
                    self._logger.warn("[测试] 未配置测试 Steam ID，跳过", user_id=user_id)
                    return
                try:
                    test_account_id = int(test_id_str)
                except ValueError:
                    self._logger.warn(f"[测试] 测试 Steam ID 无效: {test_id_str}", user_id=user_id)
                    return
                steam_id_64 = account_id_to_steam64(test_account_id)
                api_key = self._config.get_steam_config().get("steam_api_key", "")
                test_data = fetch_games(steam_id_64, api_key)
                if test_data:
                    test_ctx = _format_games_text(test_data) + "\n\n用户@你说：测试"
                    # _generate_and_publish_reply 成功后会自行标记消息为已处理
                    self._submit_reply(
                        msg, test_ctx, [], "", user_id, self._config.get_steam_prompt(), "", tag, task_id
                    )
                else:
                    self._logger.warn("[测试] 无法获取 Steam 库存数据", user_id=user_id)
                    self._publish_reply(msg, "测试失败：无法获取 Steam 库存数据", tag=tag, user_id=user_id)
                    tracker.update(task_id, "replied", reply_text="测试失败：无法获取 Steam 库存数据")
                    get_stats().record_reply_success()
                return
            elif plain == "测试 回复":
                self._logger.info(f"[测试] 管理员触发回复测试", user_id=user_id)
                test_ctx = "回复测试，成功请回复'回复成功'\n\n用户@你说：测试"
                # _generate_and_publish_reply 成功后会自行标记消息为已处理
                self._submit_reply(
                    msg, test_ctx, [], "", user_id, None, "", tag, task_id
                )
                return
            elif plain == "测试 响应":
                self._logger.info(f"[测试] 管理员触发响应测试", user_id=user_id)
                self._publish_reply(msg, "通畅", tag=tag, user_id=user_id)
                tracker.update(task_id, "replied", reply_text="通畅")
                get_stats().record_reply_success()
                with self._msg_lock:
                    self._processed_messages.add(msg_id)
                return

        result = self._access.should_allow(user_id)
        if not result["allowed"]:
            self._logger.info(f"{tag_prefix}拒绝服务: {result['reason']}", user_id=user_id)
            tracker.update(task_id, "skipped", error=result["reason"])
            return

        # access 检查通过后才计触发（被拒用户不计触发）
        get_stats().record_trigger()

        tracker.update(task_id, "context")
        context_text, image_urls = self._build_context(msg)
        if not context_text:
            # 私密帖子等不可访问情况，记录统计避免 trigger/success/fail 不匹配
            tracker.update(task_id, "skipped", error="帖子不可访问或无上下文")
            get_stats().record_reply_fail()
            return

        # 评价库存检测
        steam_config = self._config.get_steam_config()
        if steam_config.get("enabled", False):
            match = re.search(r'评价库存\s*(\d+)', text)
            if match:
                account_id = int(match.group(1))
                steam_id_64 = account_id_to_steam64(account_id)
                self._logger.info(f"检测到评价库存请求: AccountID={account_id} → SteamID64={steam_id_64}", user_id=user_id)
                from .steam_games import fetch_games, _format_games_text
                api_key = steam_config.get("steam_api_key", "")
                top_n = steam_config.get("top_games_count", 20)
                games_data = fetch_games(steam_id_64, api_key, top_n)
                if games_data:
                    games_text = _format_games_text(games_data)
                    context_text = games_text + "\n\n" + context_text
                    self._logger.info("已获取游戏库数据，评价库存无需联网搜索，直接生成回复...", user_id=user_id)
                    steam_prompt = self._config.get_steam_prompt()
                    # _generate_and_publish_reply 成功后会自行标记消息为已处理
                    self._submit_reply(
                        msg, context_text, [], "", user_id, steam_prompt, "", tag, task_id
                    )
                    return
                else:
                    self._logger.warn(f"无法获取 Steam 游戏库数据，SteamID64={steam_id_64}", user_id=user_id)
                    err_suffix = hex(random.getrandbits(16))[2:].zfill(4)
                    err_text = f"无法评价，请检查 ID 是否正确，也可能并非你的问题 ({err_suffix})"
                    self._publish_reply(msg, err_text, user_id=user_id)
                    tracker.update(task_id, "replied", reply_text=err_text)
                    get_stats().record_reply_success()
                    return

            match2 = re.search(r'推荐游戏\s*(\d+)(?:\s+(.+))?', text)
            if match2:
                account_id = int(match2.group(1))
                force_tag = match2.group(2).strip() if match2.group(2) else ""
                steam_id_64 = account_id_to_steam64(account_id)
                self._logger.info(f"检测到推荐游戏请求: AccountID={account_id}", user_id=user_id)
                from .steam_games import recommend_games
                api_key = steam_config.get("steam_api_key", "")
                rec_text = recommend_games(steam_id_64, api_key, force_tag=force_tag)
                if rec_text:
                    ctx = rec_text + "\n\n" + context_text
                    self._logger.info("已生成推荐游戏列表，调用AI生成推荐文案...", user_id=user_id)
                    steam_rec_prompt = self._config.get_steam_recommend_prompt()
                    # _generate_and_publish_reply 成功后会自行标记消息为已处理
                    self._submit_reply(
                        msg, ctx, [], "", user_id, steam_rec_prompt,
                        "（此内容为AI生成，由于成本控制库存读取并不全面，数据可能有误。）", tag, task_id
                    )
                    return
                else:
                    self._logger.warn(f"无法生成游戏推荐", user_id=user_id)
                    err_suffix = hex(random.getrandbits(16))[2:].zfill(4)
                    err_text = f"无法获取库存数据，请检查 ID 或 Steam 资料隐私设置 ({err_suffix})"
                    self._publish_reply(msg, err_text, user_id=user_id)
                    tracker.update(task_id, "replied", reply_text=err_text)
                    get_stats().record_reply_success()
                    return

        # 联网搜索（如启用）
        search_results = ""
        llm_config = self._config.get_llm_config()
        if llm_config.get("web_search", False):
            judge_config = self._config.get_llm_search_judge_config()
            if judge_config.get("enabled", False):
                self._logger.info(f"{tag_prefix}AI 判断是否需要联网搜索...", user_id=user_id)
                if not self._llm.judge_search_needed(context_text):
                    self._logger.info(f"{tag_prefix}AI 判断不需要联网搜索，跳过", user_id=user_id)
                    # _generate_and_publish_reply 成功后会自行标记消息为已处理
                    self._submit_reply(
                        msg, context_text, image_urls, "", user_id, None, "", tag, task_id
                    )
                    return

            self._logger.info(f"{tag_prefix}AI 分析帖子内容，生成搜索关键词...", user_id=user_id)
            search_query = self._llm.generate_search_query(context_text, image_urls)
            if search_query:
                self._logger.info(f"{tag_prefix}AI 生成的搜索关键词: {search_query}", user_id=user_id)
            else:
                search_query = heybox_api.plain_heybox_mention_text(msg.get("text", ""))
                search_query = re.sub(r'@\S+\s*', '', search_query).strip()
                self._logger.info(f"{tag_prefix}搜索关键词生成失败，使用原始查询: {search_query[:80]}...", user_id=user_id)

            if search_query:
                self._logger.info(f"{tag_prefix}执行联网搜索: {search_query[:80]}...", user_id=user_id)
                tracker.update(task_id, "searching")
                search_results = search_web(search_query)
                if search_results:
                    self._logger.info(f"{tag_prefix}联网搜索成功，获取到结果", user_id=user_id)
                    self._logger.info(f"{search_results}", user_id=user_id)
                else:
                    self._logger.warn(f"{tag_prefix}联网搜索未返回结果", user_id=user_id)

        # _generate_and_publish_reply 成功后会自行标记消息为已处理
        self._submit_reply(
            msg, context_text, image_urls, search_results, user_id,
            None, "", tag, task_id
        )

    def _build_context(self, msg: dict):
        """构建 AI 回复上下文。返回: (text_content, image_urls)
        get_post_tree 任何异常都返回 ("", [])，不再静默降级为无上下文回复。"""
        session = self._session
        user_id = msg.get("user", {}).get("userid", "")
        link_id = msg.get("link_id", 0)
        text = heybox_api.plain_heybox_mention_text(msg.get("text", ""))

        parts = []
        image_urls = []

        if link_id:
            # 图片数量限制（读一次配置）
            bot_config = self._config.get_bot_config()
            max_post_imgs = bot_config.get("max_post_image_num", 3)
            max_comment_imgs = bot_config.get("max_comment_image_num", 3)
            try:
                tree = heybox_api.get_post_tree(session.heybox_id, link_id, 1)
                link = tree.get("link", {})

                title = link.get("title", "")
                description = link.get("description", "")
                topic = link.get("topic_name", "")
                tags = link.get("content_tags", [])
                post_images = link.get("img_urls", [])

                if title:
                    parts.append(f"帖子标题：{title}")
                if description:
                    parts.append(f"帖子内容：{description}")
                if topic:
                    parts.append(f"帖子主题：{topic}")
                if tags:
                    parts.append(f"帖子tag：{'，'.join(tags)}")

                for img in post_images[:max_post_imgs]:
                    if img and img not in image_urls:
                        image_urls.append(img)

                comments = tree.get("comments", [])
                root_comment_id = msg.get("root_comment_id", 0)
                comment_id = msg.get("comment_id", 0)

                for group in comments:
                    for comment in group:
                        if comment["comment_id"] == root_comment_id:
                            parts.append(f"根评论内容：{comment['text']}")
                            for img in comment.get("img_urls", [])[:max_comment_imgs]:
                                if img and img not in image_urls:
                                    image_urls.append(img)
                        if comment_id and comment_id != root_comment_id and comment["comment_id"] == comment_id:
                            parts.append(f"reply评论内容：{comment['text']}")

            except Exception as e:
                err_msg = str(e)
                if "私密" in err_msg or "不可查看" in err_msg:
                    self._logger.info(f"帖子不可访问（私密/受限），跳过消息 msg_id={msg.get('message_id', 0)}", user_id=user_id)
                else:
                    self._logger.warn(f"获取帖子信息失败: {err_msg}，跳过消息 msg_id={msg.get('message_id', 0)}", user_id=user_id)
                return "", []

        if text:
            parts.append(f"用户@你说：{text}")
        else:
            parts.append("用户@了你（没有附加文字）")

        return "\n".join(parts), image_urls

    # 副号连续失败达到该次数后进入冷却
    SLOT_FAIL_THRESHOLD = 3
    # 冷却时长（秒）
    SLOT_COOLDOWN_SECONDS = 600

    def _slot_in_cooldown(self, slot: str) -> bool:
        """判断副号是否处于失败冷却期。冷却结束自动清零计数。"""
        with self._slot_fail_lock:
            info = self._slot_fail.get(slot)
            if not info:
                return False
            if time.time() < info[1]:
                return True
            self._slot_fail.pop(slot, None)
            return False

    def _record_slot_result(self, slot: str, success: bool):
        """记录副号发布结果：成功清零，连续失败达阈值进入冷却。主号不参与。"""
        if slot == "primary":
            return
        with self._slot_fail_lock:
            if success:
                self._slot_fail.pop(slot, None)
                return
            info = self._slot_fail.get(slot, [0, 0.0])
            info[0] += 1
            if info[0] >= self.SLOT_FAIL_THRESHOLD:
                info[1] = time.time() + self.SLOT_COOLDOWN_SECONDS
                self._logger.warn(
                    f"副号 {slot} 连续发布失败 {info[0]} 次，"
                    f"冷却 {self.SLOT_COOLDOWN_SECONDS // 60} 分钟内不参与回复"
                )
            self._slot_fail[slot] = info

    def _pick_reply_account(self, bot_config: dict):
        """选择本次发布使用的账号。

        返回 (api_session, heybox_id, account_label, slot)：
        api_session 为 None 表示用主会话；slot 为 "primary" 或副号 slot。

        替身模式（主号被封禁/限流时使用）：主号继续接收 @ 但停用回复。
          交叉回复开 → 替身与其他启用副号一起轮转（主号不入池）；
          交叉回复关 → 仅替身回复；
          任何兜底回退主号都会醒目告警（主号可能封禁中）。
        """
        session = self._session
        primary = (None, session.heybox_id, "主号", "primary")

        # 安全读取 accounts（通过 copy 避免并发修改）
        account_dict = dict(getattr(session, "accounts", {}))

        def slot_usable(acc: dict) -> bool:
            return bool(acc.get("cookies") and acc.get("heybox_id") and acc.get("enabled", True))

        multi_account = bot_config.get("multi_account", False)
        standby_mode = bot_config.get("standby_mode", False)
        standby_slot = bot_config.get("standby_slot", "") if standby_mode else ""

        # 副号轮转池：启用且未冷却（按 slot 排序保证顺序稳定）
        pool = sorted(
            k for k, v in account_dict.items()
            if slot_usable(v) and not self._slot_in_cooldown(k)
        )

        def pick_standby():
            """替身独用：可用返回 (四元组, None)，不可用返回 (None, 具体原因)。"""
            if not standby_slot:
                return None, "未选择替身账号"
            acc = account_dict.get(standby_slot)
            if not acc:
                return None, f"替身账号 {standby_slot} 不存在（可能已被移除）"
            if not acc.get("enabled", True):
                return None, f"替身「{acc.get('nickname', standby_slot)}」已被禁用"
            if not (acc.get("cookies") and acc.get("heybox_id")):
                return None, f"替身账号 {standby_slot} 登录信息不完整"
            if self._slot_in_cooldown(standby_slot):
                return None, f"替身「{acc.get('nickname', standby_slot)}」连续发布失败，冷却中"
            s = heybox_api.get_account_session(standby_slot)
            if not s:
                return None, f"替身账号 {standby_slot} 会话未注册（可能重启后未加载或已被移除）"
            nickname = acc.get("nickname", standby_slot)
            return (s, acc["heybox_id"], f"替身「{nickname}」", standby_slot), None

        # === 替身模式：主号停用回复（仍负责接收 @）===
        if standby_mode:
            if multi_account and pool:
                # 交叉回复开：替身与其他副号一起轮转，主号不入池
                with self._msg_lock:
                    slot = pool[self._account_cycle % len(pool)]
                    self._account_cycle = (self._account_cycle + 1) % 100000
                api_session = heybox_api.get_account_session(slot)
                if api_session:
                    acc = account_dict[slot]
                    nickname = acc.get("nickname", slot)
                    return api_session, acc["heybox_id"], f"副号「{nickname}」", slot
                self._logger.warn(f"副号 {slot} Session 不存在，尝试兜底账号")
            # 交叉回复关（或池空/Session 缺失）：仅替身
            picked, reason = pick_standby()
            if picked:
                return picked
            self._logger.warn(
                f"替身模式：{reason}，回退主号——"
                f"主号可能处于封禁状态，回复可能失败或仅自己可见"
            )
            return primary

        # === 非替身模式：维持现状 ===
        if not multi_account:
            return primary
        if session.cookies and session.heybox_id:
            pool.append("primary")
        if not pool:
            return primary

        with self._msg_lock:
            slot = pool[self._account_cycle % len(pool)]
            self._account_cycle = (self._account_cycle + 1) % 100000

        if slot == "primary":
            return primary
        api_session = heybox_api.get_account_session(slot)
        if not api_session:
            self._logger.warn(f"副号 {slot} Session 不存在，回退主号")
            return primary
        acc = account_dict[slot]
        nickname = acc.get("nickname", slot)
        return api_session, acc["heybox_id"], f"副号「{nickname}」", slot

    def _auto_like(self, api_session, heybox_id: str, msg: dict, account_label: str, tag_prefix: str, user_id: str = ""):
        """回复成功后的自动点赞：仅使用执行回复的同一账号。

        触发消息是评论（评论里 @ 机器人）→ 赞该评论；
        触发消息是帖子（发帖 @ 机器人）→ 赞该帖子。
        点赞失败只记日志，不影响回复主流程。
        """
        if not self._config.get_bot_config().get("auto_like", False):
            return
        link_id = msg.get("link_id", 0)
        comment_id = msg.get("comment_id", 0)
        is_post = msg.get("is_post", False)
        try:
            if not is_post and comment_id:
                heybox_api.like_comment(heybox_id, comment_id, session=api_session)
                self._logger.info(f"{tag_prefix}{account_label}已点赞触发评论", user_id=user_id)
            elif link_id:
                heybox_api.like_post(heybox_id, link_id, session=api_session)
                self._logger.info(f"{tag_prefix}{account_label}已点赞触发帖子", user_id=user_id)
        except Exception as e:
            self._logger.warn(f"{tag_prefix}{account_label}自动点赞失败（回复已成功，不受影响）: {e}", user_id=user_id)

    def _publish_reply(self, msg: dict, reply_text: str, tag: str = "", user_id: str = ""):
        """发布回复到小黑盒。支持多账号轮转和替身模式。"""
        session = self._session
        link_id = msg.get("link_id", 0)
        is_post = msg.get("is_post", False)
        root_comment_id = msg.get("root_comment_id", 0)
        comment_id = msg.get("comment_id", 0)
        tag_prefix = f"[{tag}] " if tag else ""

        if not link_id:
            self._logger.error(f"{tag_prefix}无法发布回复: 帖子 ID 为空", user_id=user_id)
            return

        bot_config = self._config.get_bot_config()
        api_session, pub_heybox_id, account_label, slot = self._pick_reply_account(bot_config)
        if account_label.startswith("替身"):
            self._logger.info(
                f"{tag_prefix}替身模式：主号已停用回复，使用 {account_label} 替代回复",
                user_id=user_id,
            )

        try:
            if is_post or root_comment_id == 0:
                result = heybox_api.comment_post(pub_heybox_id, link_id, reply_text, session=api_session)
            elif comment_id == root_comment_id:
                result = heybox_api.comment_root(pub_heybox_id, link_id, root_comment_id, reply_text, session=api_session)
            else:
                reply_id = comment_id if comment_id else root_comment_id
                result = heybox_api.comment_reply(pub_heybox_id, link_id, root_comment_id, reply_id, reply_text, session=api_session)
            self._record_slot_result(slot, True)
            self._logger.info(f"{tag_prefix}{account_label}回复成功", user_id=user_id)
            self._auto_like(api_session, pub_heybox_id, msg, account_label, tag_prefix, user_id)
        except heybox_api.ReloginError as e:
            # 登录态失效：副号/替身自动摘除；主号失效则按普通失败处理
            if api_session and slot != "primary":
                self._record_slot_result(slot, False)
                self._logger.warn(f"副号/替身 login 失效，自动移除 ({e})", user_id=user_id)
                heybox_api.remove_account_session(slot)
                if slot in self._session.accounts:
                    # 风控自动禁用：不清除账号，仅设置 enabled=False
                    if bot_config.get("auto_disable_alt_on_risk", True):
                        self._session.accounts[slot]["enabled"] = False
                        self._session.accounts[slot]["disabled_at"] = time.time()
                        self._session.save()
                        self._logger.warn(f"副号 {slot} 因风控已自动禁用，将不再参与轮转", user_id=user_id)
                    else:
                        self._session.accounts.pop(slot, None)
                        self._session.save()
                # 选择重试账号：替身模式下主号停用回复，优先池中下一个可用副号
                retry_session = None
                retry_heybox_id = session.heybox_id
                retry_label = "主号"
                if bot_config.get("standby_mode", False):
                    account_dict = dict(getattr(session, "accounts", {}))
                    for alt_slot in sorted(account_dict.keys()):
                        if alt_slot == slot:
                            continue
                        acc = account_dict[alt_slot]
                        if not (acc.get("cookies") and acc.get("heybox_id") and acc.get("enabled", True)):
                            continue
                        if self._slot_in_cooldown(alt_slot):
                            continue
                        alt_session = heybox_api.get_account_session(alt_slot)
                        if alt_session:
                            retry_session = alt_session
                            retry_heybox_id = acc["heybox_id"]
                            retry_label = f"副号「{acc.get('nickname', alt_slot)}」"
                            break
                    if retry_session is None:
                        self._logger.warn(
                            "替身模式：无其他可用副号，回退主号重试——主号可能处于封禁状态",
                            user_id=user_id,
                        )
                try:
                    if is_post or root_comment_id == 0:
                        heybox_api.comment_post(retry_heybox_id, link_id, reply_text, session=retry_session)
                    elif comment_id == root_comment_id:
                        heybox_api.comment_root(retry_heybox_id, link_id, root_comment_id, reply_text, session=retry_session)
                    else:
                        heybox_api.comment_reply(retry_heybox_id, link_id, root_comment_id, reply_id if comment_id else root_comment_id, reply_text, session=retry_session)
                    self._logger.info(f"{tag_prefix}{retry_label}回复成功（失效账号已移除）", user_id=user_id)
                    self._auto_like(retry_session, retry_heybox_id, msg, retry_label, tag_prefix, user_id)
                    return
                except Exception as retry_err:
                    self._logger.error(f"{tag_prefix}{retry_label}重试也失败: {retry_err}", user_id=user_id)
                    raise retry_err
            self._logger.error(f"{tag_prefix}发布回复失败（登录态失效）: {e}", user_id=user_id)
            raise
        except Exception as e:
            self._record_slot_result(slot, False)
            self._logger.error(f"{tag_prefix}发布回复失败: {e}", user_id=user_id)
            raise

    def get_status(self) -> dict:
        """获取当前运行状态。"""
        bot_config = self._config.get_bot_config()
        status = {
            "running": self._running,
            "current_interval": self._current_interval,
            "init_wait_time": bot_config.get("init_wait_time", 10),
            "max_wait_time": bot_config.get("max_wait_time", 60),
            "increment": bot_config.get("increment", 10),
            "multi_account": bot_config.get("multi_account", False),
            "standby_mode": bot_config.get("standby_mode", False),
            "standby_slot": bot_config.get("standby_slot", ""),
        }
        if self._wait_duration > 0:
            elapsed = time.time() - self._wait_start_time
            remaining = max(0, self._wait_duration - elapsed)
            status["wait_remaining"] = int(remaining)
            status["wait_total"] = self._wait_duration
        return status


# 全局实例
_bot_instance = None


def get_bot_engine() -> BotEngine:
    global _bot_instance
    if _bot_instance is None:
        _bot_instance = BotEngine()
    return _bot_instance