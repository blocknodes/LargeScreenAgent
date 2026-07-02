# slime —— 极简离线版影视问答 Agent

单文件、自包含的影视问答 Agent。只保留核心流程：

```
query → 工具循环（web_search / db_search / get_weather）→ 最终回答
```

支持 **Experience Learning 闭环**：从历史失败中自动提取经验规则，注入 prompt 提升表现。

不需要启动任何服务，命令行直接运行。

## 目录结构

```
slime/
├── slime.py               # 主程序（自包含，含工具定义 + Agent 循环）
├── slime_app.py           # 极简 Web 应用（单文件，标准库 http.server，复用 slime.ask）
├── slime_prompt.txt       # 默认 system prompt（可直接编辑，或用 SLIME_PROMPT_FILE 指定）
├── slime_prompt_slim.txt  # 精简版 prompt（去冗余 + 工具纪律/排序硬约束，供 A/B）
├── run_benchmark.py       # 跑分评测（并发、hit@1/3/5/all、轨迹 dump）
├── sample_benchmark.py    # 从 benchmark 随机抽样 N 条，保存为新 CSV
├── experience_store.py    # 经验库管理模块（加载/保存/检索/渲染经验条目）
├── learn_from_run.py      # 从轨迹自动学习经验（规则模板匹配，离线）
├── llm_learn.py           # 用强模型从轨迹智能总结经验（调 LLM，规则随案例泛化）
├── mine_experience.py     # 从轨迹挖失败模式、蒸馏经验（旧版，输出 markdown 审阅稿）
├── run_experience_loop.py # Experience Learning 自动迭代脚本（一键闭环）
├── manage_experience.py   # 经验库管理 CLI（list/add/disable/export）
├── experience/            # 经验库存储目录
│   └── rules.jsonl        # 结构化经验条目（JSONL 格式）
├── benchmark_0601.csv     # benchmark 数据（query,label）
├── requirements.txt       # 依赖：openai / httpx / python-dotenv
├── .env.example           # 配置模板
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

## Experience Learning 闭环

从历史失败轨迹中自动提取经验规则，注入 prompt 提升表现。完整闭环：

```
跑分(baseline) → 学习(提取经验) → 注入经验再跑分 → 对比 → 再学习 ...
```

### 快速上手

```bash
# 一键闭环（baseline + 学习 + 再跑分 + 对比）
python run_experience_loop.py --strong runs/exp2 --csv benchmark_sample100.csv -c 8

# 或分步执行：

# 1. 跑 baseline
python run_benchmark.py benchmark_sample100.csv -c 8 -d runs/baseline

# 2. 从失败中学习经验
python learn_from_run.py --weak runs/baseline --strong runs/exp2

# 3. 带经验跑分
python run_benchmark.py benchmark_sample100.csv -c 8 -d runs/with_exp --experience experience/rules.jsonl
# 或用环境变量：
SLIME_EXPERIENCE_FILE=experience/rules.jsonl python run_benchmark.py benchmark_sample100.csv -c 8 -d runs/with_exp
```

### 两种经验提取方式

| 脚本 | 方式 | 是否调 LLM | 特点 |
|------|------|-----------|------|
| `learn_from_run.py` | 规则模板匹配 | 否（离线） | 快、稳定、可审计；规则文本固定 |
| `llm_learn.py` | 强模型智能总结 | 是 | 让"牛逼的模型"分析轨迹自己归纳；规则随案例泛化 |

用强模型总结经验（跑完一轮 benchmark 后）：

```bash
# 用默认模型（.env 的 LLM_MODEL）总结失败案例
python llm_learn.py --weak runs/baseline --strong runs/exp2

# 指定一个更强的模型来做总结
python llm_learn.py --weak runs/baseline --model deepseek-r1 --store experience/llm.jsonl

# 总结模型走另一套服务商/凭证（不影响跑分用的 LLM_*）
python llm_learn.py --weak runs/baseline --model deepseek-r1 \
    --api-key sk-xxx --api-url https://api.deepseek.com
# 或用独立环境变量（任一留空则回退到对应 LLM_*）：
LEARN_API_KEY=sk-xxx LEARN_API_URL=https://api.deepseek.com LEARN_MODEL=deepseek-r1 \
    python llm_learn.py --weak runs/baseline

# 控制规模：最多 40 条失败案例，每批 20 条喂给模型
python llm_learn.py --weak runs/baseline --max-cases 40 --batch-size 20

# 预览将发送给模型的 prompt，不实际调用
python llm_learn.py --weak runs/baseline --dry-run

# 一键闭环中切换为 LLM 总结（LEARN_* 环境变量会自动透传给子进程）
python run_experience_loop.py --strong runs/exp2 --llm --llm-model deepseek-r1 -c 8
```

`llm_learn.py` 把失败轨迹（含 weak/strong 对照的紧凑工具序列）喂给强模型，让它输出
结构化 JSON 经验规则，解析去重后写入同一个经验库，注入方式与模板版完全一致。

### 经验库管理

```bash
python manage_experience.py list                     # 列出所有经验
python manage_experience.py show                     # 预览注入 prompt 效果
python manage_experience.py show --examples          # 含对照实例
python manage_experience.py stats                    # 统计信息
python manage_experience.py add "规则文本" --signal repeat_call --priority 8   # 手动添加
python manage_experience.py disable <id>             # 禁用某条（A/B 测试）
python manage_experience.py enable <id>              # 重新启用
python manage_experience.py export --format prompt   # 导出为 prompt 片段
python manage_experience.py export --format json     # 导出为 JSON
```

### 多轮迭代

```bash
# 3 轮迭代：每轮从上一轮的失败中增量学习
python run_experience_loop.py --iterations 3 --strong runs/exp2 -c 8

# 已有 baseline 时跳过第 1 步
python run_experience_loop.py --baseline runs/exp1 --strong runs/exp2 --iterations 2
```

### 经验条目格式

经验以 JSONL 格式存储在 `experience/rules.jsonl`，每条包含：

| 字段 | 说明 |
|------|------|
| `id` | 唯一标识（自动生成） |
| `rule` | 规则文本（注入 system prompt） |
| `signal` | 触发信号（repeat_call / db_error / no_stop_after_match 等） |
| `priority` | 优先级 1-10（越高越靠前注入） |
| `enabled` | 是否启用（方便 A/B 开关单条经验） |
| `examples` | 对照实例（weak/strong 轨迹对比） |
| `source` | 来源描述 |

### 工作原理

1. **信号检测**：分析失败轨迹，识别行为模式（重复调用、db 报错、命中后不收尾、剧情词当 titles 等）
2. **规则生成**：按失败模式出现频率生成优先级化的规则，附带 weak/strong 对照实例
3. **Prompt 注入**：运行时自动将启用的规则追加到 system prompt 末尾
4. **增量学习**：新一轮的失败可继续学习，去重后追加（`--append`）
5. **A/B 验证**：同一 benchmark + 同一模型，有/无经验注入对比指标

## 配置项（.env / 环境变量）

| 变量 | 说明 |
|------|------|
| `LLM_API_KEY` | LLM API Key |
| `LLM_API_URL` | LLM API 地址（自动追加 `/v1`，默认 `https://api.deepseek.com`） |
| `LLM_MODEL` | 模型名（默认 `deepseek-v4-pro`） |
| `LLM_MAX_TOKENS` | 最大生成 token（默认 65536） |
| `LEARN_API_KEY` / `LEARN_API_URL` / `LEARN_MODEL` | 经验总结模型的独立凭证（`llm_learn.py`）；任一留空则回退到对应的 `LLM_*` |
| `WEB_SEARCH_API_KEY` / `WEB_SEARCH_URL` | web 搜索接口 |
| `DB_SEARCH2_URL` | 媒资库检索接口 |
| `WEATHER_API_KEY` | 天气接口 Key（可选） |
| `SLIME_PROMPT_FILE` | system prompt 文件路径（默认同目录 `slime_prompt.txt`） |
| `SLIME_EXPERIENCE_FILE` | 经验库文件路径（为空则不注入经验） |
| `SLIME_APP_HOST` / `SLIME_APP_PORT` | Web 应用监听地址/端口（默认 `127.0.0.1` / `8099`） |

> 说明：这里的「离线」指无需启动服务进程，脚本运行时仍会调用上述 LLM 与工具接口（需要网络）。

## 与完整服务版的差异

仅保留最小 Agent 循环，去掉了服务端的：流式输出、trace/usage 记账、强制重试与
db_search 提醒、连续失败熔断、web_search 结果 top-K 过滤、多厂商 thinking 配置、
模型预设表等。工具 schema 与默认 system prompt 与线上保持一致。
