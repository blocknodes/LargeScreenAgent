# slime —— 极简离线版影视问答 Agent

单文件、自包含的影视问答 Agent。只保留核心流程：

```
query → 工具循环（web_search / db_search / get_weather）→ 最终回答
```

不需要启动任何服务，命令行直接运行。

## 目录结构

```
slime/
├── slime.py             # 主程序（自包含，含工具定义 + Agent 循环）
├── slime_app.py         # 极简 Web 应用（单文件，标准库 http.server，复用 slime.ask）
├── slime_prompt.txt     # 默认 system prompt（可直接编辑，或用 SLIME_PROMPT_FILE 指定）
├── slime_prompt_slim.txt# 精简版 prompt（去冗余 + 工具纪律/排序硬约束，供 A/B）
├── run_benchmark.py     # 跑分评测（并发、hit@1/3/5/all、轨迹 dump）
├── sample_benchmark.py  # 从 benchmark 随机抽样 N 条，保存为新 CSV
├── mine_experience.py   # 从轨迹挖失败模式、蒸馏经验（experience learning 第一步）
├── benchmark_0601.csv   # benchmark 数据（query,label）
├── requirements.txt     # 依赖：openai / httpx / python-dotenv
├── .env.example         # 配置模板
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

## Web 应用（slime_app.py）

单文件、零额外依赖（仅 Python 标准库 `http.server`）的网页版，复用 `slime.ask()`，
把每轮思考 / 工具调用 / 工具返回 / 最终回答渲染成轨迹卡片 + 推荐媒资卡片。

```bash
python slime_app.py                          # 默认 http://127.0.0.1:8099
SLIME_APP_PORT=9000 python slime_app.py      # 换端口
SLIME_APP_HOST=0.0.0.0 python slime_app.py   # 允许外部访问（无鉴权，注意安全）
SLIME_PROMPT_FILE=slime_prompt_slim.txt python slime_app.py   # 用精简版 prompt
```

> 默认绑 `127.0.0.1` 仅本机访问且**无鉴权**；`SLIME_APP_HOST/PORT` 也可写进 `.env`。

## 跑分评测（run_benchmark.py）

读取 benchmark CSV（每行 `query,label`，label 可用 ` / ` 分隔多个可接受答案），并发跑
`slime.ask`，输出指标与每条 query 的详细轨迹，全部落到一个输出目录。

指标：
- `ok`：label 任一别名出现在回答任意位置（宽松，文本级）
- `hit@1/3/5`：正确片名在【推荐媒资卡片】排在前 1/3/5 位（标题归一化后包含匹配，容忍"片名（年份/演员）"等装饰）
- `hit@all`：命中卡片任意名次

```bash
python run_benchmark.py                       # 全量，并发 4
python run_benchmark.py -c 8                   # 并发 8
python run_benchmark.py --shuffle -n 100       # 随机抽 100 条
python run_benchmark.py --shuffle --seed 42    # 固定随机种子，可复现
python run_benchmark.py -d runs/exp1           # 指定输出目录
python run_benchmark.py --no-dump              # 不写 traces/（仍写 config/result/summary）
```

输出目录（默认 `benchmark_out/run_<时间戳>/`）：

```
<out-dir>/
├── config.json    # 本次配置：模型/采样参数/接口地址 + 相关环境变量（密钥脱敏）
├── result.csv     # 逐条明细（ok/rank/hit@1/3/5/all、轮次、token）
├── summary.json   # 汇总指标
└── traces/        # {idx}.txt（可读轨迹）+ traces.jsonl（结构化）
```

A/B 对比（切 prompt 跑同一批样本）：

```bash
python run_benchmark.py benchmark_0601.csv -c 8 -d runs/p_full
SLIME_PROMPT_FILE=slime_prompt_slim.txt python run_benchmark.py benchmark_0601.csv -c 8 -d runs/p_slim
```

## 抽样与经验挖掘

```bash
# 从 benchmark 随机抽 N 条存成新 CSV（多次/多 prompt 用同一固定样本对比）
python sample_benchmark.py -n 100 --seed 42        # → benchmark_sample100.csv

# 对比一弱一强两次 run，挖失败模式（循环/报错/塞剧情词/不收尾）并蒸馏成 prompt 规则草稿
python mine_experience.py --weak runs/exp_a --strong runs/exp_b --out experience/ab
# 产物：experience/ab/failures.jsonl（结构化失败样本）+ insights.md（占比 + 规则草稿 + 对照实例）
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
| `SLIME_APP_HOST` / `SLIME_APP_PORT` | Web 应用监听地址/端口（默认 `127.0.0.1` / `8099`） |

> 说明：这里的「离线」指无需启动服务进程，脚本运行时仍会调用上述 LLM 与工具接口（需要网络）。

## 与完整服务版的差异

仅保留最小 Agent 循环，去掉了服务端的：流式输出、trace/usage 记账、强制重试与
db_search 提醒、连续失败熔断、web_search 结果 top-K 过滤、多厂商 thinking 配置、
模型预设表等。工具 schema 与默认 system prompt 与线上保持一致。
