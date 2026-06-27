# slime —— 极简离线版影视问答 Agent

单文件、自包含的影视问答 Agent。只保留核心流程：

```
query → 工具循环（web_search / db_search / get_weather）→ 最终回答
```

不需要启动任何服务，命令行直接运行。

## 目录结构

```
slime/
├── slime.py           # 主程序（自包含，含工具定义 + Agent 循环）
├── slime_prompt.txt   # system prompt（可直接编辑，或用 SLIME_PROMPT_FILE 指定其他文件）
├── requirements.txt   # 依赖：openai / httpx / python-dotenv
├── .env.example       # 配置模板
└── README.md
```

## 安装

```bash
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env 填入 LLM、web_search、db_search 等接口的 Key 和地址
```

## 运行

```bash
python slime.py "刘昊然演的一个邮差躲在照相馆里的电影"
python slime.py                       # 不带参数则交互式输入
```

## 调试模式

中间信息一律输出到 stderr，stdout 只保留最终回答，方便管道使用。

| 模式 | 命令行 | 环境变量 | 输出内容 |
|------|--------|----------|----------|
| 普通（默认） | （默认开启 debug） | — | 调试日志 + 最终回答 |
| 安静 | `--quiet` / `-q` | `SLIME_DEBUG=0` | 仅最终回答 |
| verbose | `--verbose` | `SLIME_VERBOSE=1` | debug 全部 + 每轮 LLM 完整输入/原始输出（不截断） |

调试日志**默认开启**，输出到 stderr，stdout 仍只保留最终回答。`--verbose` 隐含 debug。

```bash
python slime.py "推荐几部悬疑剧"                # 默认就会打印调试过程
python slime.py -q "推荐几部悬疑剧"             # 安静模式，只输出答案
python slime.py --verbose "..." 2>trace.log    # 完整 LLM 输入输出存文件
python slime.py "..." --debug-stdout >all.txt  # 调试+答案一起重定向到一个文件
```

## 日志文件

每次运行**默认**把完整日志（配置、每轮思考/工具调用/工具返回、最终回答）写入
`logs/slime_<时间戳>.log`。控制台为了可读会截断超长字段，但**日志文件始终是完整内容**。

```bash
python slime.py "..."                          # 默认写 logs/slime_YYYYmmdd_HHMMSS.log
python slime.py "..." --log-file run.log       # 指定日志文件（追加写）
SLIME_LOG_FILE=run.log python slime.py "..."   # 用环境变量指定
python slime.py "..." --no-log                 # 不写日志文件
```

## 配置项（.env / 环境变量）

| 变量 | 说明 |
|------|------|
| `LLM_API_KEY` | LLM API Key |
| `LLM_API_URL` | LLM API 地址（自动追加 `/v1`，默认 `https://api.deepseek.com`） |
| `LLM_MODEL` | 模型名（默认 `deepseek-v4-pro`） |
| `LLM_MAX_TOKENS` | 最大生成 token（默认 65536） |
| `WEB_SEARCH_API_KEY` / `WEB_SEARCH_URL` | web 搜索接口 |
| `DB_SEARCH2_URL` | 媒资库检索接口 |
| `WEATHER_API_KEY` | 天气接口 Key（可选） |
| `SLIME_PROMPT_FILE` | system prompt 文件路径（默认同目录 `slime_prompt.txt`） |

> 说明：这里的「离线」指无需启动服务进程，脚本运行时仍会调用上述 LLM 与工具接口（需要网络）。

## 与完整服务版的差异

仅保留最小 Agent 循环，去掉了服务端的：流式输出、trace/usage 记账、强制重试与
db_search 提醒、连续失败熔断、web_search 结果 top-K 过滤、多厂商 thinking 配置、
模型预设表等。工具 schema 与默认 system prompt 与线上保持一致。
