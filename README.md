# 小黑盒 AI 自动回复机器人 · Web Console

基于 Flask 的小黑盒社区 AI 自动回复机器人，提供「黑盒指挥舱」风格的 Web 控制台，支持可视化配置、实时日志监控、Steam 库存评价、联网搜索增强等功能。

---

## 核心功能

### 智能自动回复
- 自动轮询小黑盒 @消息、评论、私信，调用大语言模型生成符合社区风格的回复。
- 支持白名单模式 / 频率限制模式，精确控制回复范围。
- 递增间隔策略：无新消息时自动延长轮询间隔，检测到消息后重置。
- 支持评论监控与自动回复，内置「评价库存 + AccountID」「推荐游戏 + AccountID」指令识别。
- 处理失败的消息自动在下一轮重试一次，仍失败才标记失败丢弃。

### 多模型支持
- 支持 OpenAI / DeepSeek / Anthropic / 通义千问 / Kimi / 百度千帆 / 智谱 / Gemini 等 15+ 主流 AI 提供商。
- 四套独立模型配置：主回复模型、搜索关键词提取模型、搜索需求判断模型、Steam 评价模型。
- 切换提供商时自动记忆并恢复对应的 API Key（`vendor_keys`）。
- 支持流式输出，实时显示 AI 思考过程。
- 各模型 Token 消耗统计（按模型分列的文本列表）。
- 支持自定义 API 路径（`api_path`），适配非标准 OpenAI 兼容端点。

### 联网搜索增强
- 百度千帆 AI 搜索 / Tavily Search 双引擎支持。
- 自动故障转移模式：主引擎失效时自动切换备用引擎。
- AI 判断是否需要搜索 + 关键字启发式兜底，避免误判。
- 搜索结果折叠展示于日志面板。

### Steam 库存评价
- 检测到「评价库存 + Steam AccountID」消息时，自动爬取 Steam 游戏库并生成个性化评价。
- 基于本地 Steam 游戏榜单数据库，增强推荐与标签补充。
- 支持自定义 Steam 评价提示词。

### Steam 游戏榜单管理
- 本地维护 Steam 热门标签游戏榜单数据库 `data/steam_local_db.json`。
- 按分类每日独立缓存，仅非今日更新时重新爬取。
- 每个分类爬取完成后立即持久化，中途中断后可续爬。
- 支持 `steam.auto_scrape` 自动后台更新开关。
- 提供手动触发爬取、刷新榜单状态、查看各分类更新情况。
- 爬取过程降频处理，失败自动重试，降低触发反爬风险。

### 实时仪表盘
- 触发次数 / 成功次数 / 失败次数 / 搜索次数四维统计卡片。
- 各模型 Token 消耗统计（按模型分列的文本列表，含 tokens 与调用次数）。
- Steam 库存评价等级分布饼图（SSS~D）。
- 机器人运行时长、状态监控。
- 数字滚动动画，2 秒自动刷新。

### 对话任务面板
- 每条 @ 消息自动生成任务卡片，实时展示「排队等待 → 获取上下文 → 联网搜索 → 生成回复 → 发布」全流程状态。
- 支持按关键词 / UID / 日期 / 状态过滤查询。

### 实时日志系统
- SSE（Server-Sent Events）实时推送日志到 Web 控制台。
- INFO / WARN / ERROR 三级颜色高亮。
- 日志按日期分文件存储：`data/bot_YYYY-MM-DD.log`。
- 内存环形缓冲区保留最近 2000 条日志，支持清空、导出（导出优先取当日磁盘日志完整内容）。
- SSE 连接建立后立即推送最近 100 条历史日志，随后仅实时推送新日志。
- 日志折叠展示：
  - `[SEARCH]` 搜索结果可折叠展开。
  - `[Steam 爬取]` 连续日志合并为一条折叠组。
  - `[自检] 正在检测 Steam 游戏榜单时效...` 详情默认折叠。

### 账号管理
- 小黑盒 APP 扫码登录，会话自动持久化。
- 启动时自动检测登录状态有效性。
- 支持主号 + 多个副号切换与管理，多账号交叉轮转回复。
- 替身模式：主号被封禁/限流时，指定副号代替主号回复（主号仍负责接收 @）。
- 副号连续发布失败自动冷却，登录态失效自动禁用，降低风控风险。
- 提供检查状态、退出登录、重置配置功能。

### 黑盒指挥舱主题
- 左侧边栏控制中心 + 右侧终端日志主区域。
- 深色终端风格，统一的暗色主题组件（按钮、开关、输入框、卡片）。
- 响应式布局，适配不同屏幕尺寸。

---

## 快速开始

### 方式一：下载 Release
从 [Releases](../../releases) 页面下载最新压缩包，解压后按下方「启动服务」步骤执行即可。

### 方式二：克隆源码
```bash
git clone https://github.com/shrimp9527/heybox-bot-web-multi-redesign.git
cd heybox-bot-web-multi-redesign
```

### 1. 环境要求
- Python 3.9+
- pip

### 2. 安装依赖
```bash
pip install -r requirements.txt
```

### 3. 启动服务
```bash
python app.py
```

也可以直接使用启动脚本（自动检查 Python 与依赖）：Windows 双击 `启动机器人-multi.bat`，Linux 执行 `./启动机器人-multi.sh`。

服务启动后，浏览器访问 `http://127.0.0.1:5500`。

### 4. 首次配置
1. 点击「扫码登录」使用小黑盒 APP 扫码。
2. 在「AI 模型」标签页选择提供商并填写 API Key。
3. （可选）开启「联网搜索」并配置搜索引擎。
4. （可选）开启「Steam 库存评价」并配置 Steam API Key。
5. 点击「▶ 启动」开始自动回复。

---

## 配置说明

### 机器人配置
- **回复模式**：白名单 / 频率限制。
- **轮询间隔**：无消息时自动递增，有消息时重置。
- **内置指令**：`评价库存 <AccountID>`、`推荐游戏 <AccountID> [标签]`（硬编码识别，无需配置触发词）。
- **账号管理**：主号与副号切换、替身模式、检查状态、退出登录、重置配置。

### AI 模型配置
- 配置主回复模型、搜索关键词提取模型、搜索判断模型、Steam 评价模型。
- 独立配置各模型的 API Key、Base URL、API 路径、模型名称、最大 Token 等参数。

### 提示词配置
- 自定义 AI 系统提示词。
- 自定义 Steam 库存评价提示词。

### Steam 配置
- `enabled`：是否启用 Steam 库存评价。
- `steam_api_key`：Steam Web API Key。
- `top_games_count`：推荐展示游戏数量（默认 20）。
- `auto_scrape`：程序启动后是否自动后台更新榜单（默认 true）。

---

## 安全特性
- 敏感配置（API Key / 会话令牌）存储在 `data/` 目录，已加入 `.gitignore`。
- 所有 `/api/` 接口均需校验随机生成的 API Token（每次启动重新生成，经请求头或 HttpOnly Cookie 下发）。
- 访问控制：白名单模式 + 频率限制，防止滥用。
- 本地运行，不对外暴露服务。

## 安全提示
- `data/` 目录下的 `config.json` 保存有**明文 API Key**，`session.json` 保存有小黑盒**登录会话 Cookie**，二者泄露即等于账号与密钥泄露。
- 请勿将 `data/` 目录打包分享、上传到公开仓库或发送给他人。
- 如需备份，请妥善加密保管；分享项目时只分发代码与 `config-example.json`。

---

## 项目结构

```
heybox-bot-web-multi-redesign/
├── app.py                      # Flask 主入口 + API 路由
├── requirements.txt            # Python 依赖
├── config-example.json         # 配置文件示例
├── 启动机器人-multi.bat         # Windows 启动脚本
├── 启动机器人-multi.sh          # Linux 启动脚本
├── config/
│   └── config_manager.py       # 配置管理器
├── core/
│   ├── bot_engine.py           # 机器人引擎（轮询/调度）
│   ├── llm_client.py           # LLM 统一调用客户端
│   ├── web_search.py           # 联网搜索模块
│   ├── heybox_api.py           # 小黑盒 API 封装
│   ├── heybox_sign.py          # 小黑盒请求签名
│   ├── session_manager.py      # 会话管理器
│   ├── access_control.py       # 访问控制
│   ├── stats.py                # 统计管理器
│   ├── task_tracker.py         # 对话任务面板追踪
│   ├── json_store.py           # JSON 原子读写工具
│   └── steam_games.py          # Steam 本地榜单数据库管理
├── logger/
│   └── log_manager.py          # 日志管理器（内存缓冲 + 文件 + SSE）
├── data/                       # 运行数据目录（不入库）
│   ├── bot_YYYY-MM-DD.log      # 按日期存放的日志文件
│   ├── config.json             # 用户配置（含明文 API Key，勿外传）
│   ├── session.json            # 登录会话（含 Cookie，勿外传）
│   ├── steam_local_db.json     # Steam 本地榜单数据库
└── web/
    ├── templates/
    │   └── index.html          # Web 控制台页面
    └── static/
        ├── css/style.css       # 样式表
        └── js/
            ├── main.js         # 前端逻辑
            └── vendor/         # 第三方前端库（qrcode.min.js）
```

---

## 主要依赖

| 库 | 用途 |
|---|------|
| Flask | Web 框架 |
| Flask-CORS | 跨域支持 |
| Requests | HTTP 客户端 |
| Tavily-Python | Tavily 搜索引擎 |
| Pillow | 图片处理（多模态） |

---

## 更新日志

详见 [docs/CHANGELOG.md](docs/CHANGELOG.md)。

---

本项目仅供学习交流使用。
