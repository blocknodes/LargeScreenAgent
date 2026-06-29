"""
极简离线版影视问答 Agent（slime）—— 自包含单文件。

只保留核心流程：query → 工具循环（web_search / db_search / get_weather）→ 最终回答。
不依赖项目内的 app/ 包，也不需要启动任何服务。直接命令行运行：

    python slime.py "刘昊然演的一个邮差躲在照相馆里的电影"
    python slime.py              # 不带参数则交互式输入
    python slime.py --debug "..."    # 打印每轮思考/工具调用/工具返回等中间信息
    python slime.py --verbose "..."  # 在 debug 基础上，额外打印每轮 LLM 的完整输入与原始输出

依赖：openai、httpx、python-dotenv（见 requirements.txt）。

配置（从 .env 或环境变量读取）：
    LLM_API_KEY        LLM API Key
    LLM_API_URL        LLM API 地址（默认 https://api.deepseek.com，自动追加 /v1）
    LLM_MODEL          模型名（默认 deepseek-v4-pro）
    LLM_MAX_TOKENS     最大生成 token（默认 65536）
    WEB_SEARCH_API_KEY / WEB_SEARCH_URL   web 搜索接口
    DB_SEARCH2_URL     媒资库检索接口
    WEATHER_API_KEY    天气接口 Key（可选）
    SLIME_PROMPT_FILE  system prompt 文件路径（默认同目录 slime_prompt.txt）
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_API_URL = os.getenv("LLM_API_URL", "https://api.deepseek.com")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-v4-pro")
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "65536"))
# 采样参数：留空则不传，使用模型默认值。想降低随机性可设 LLM_TEMPERATURE=0。
LLM_TEMPERATURE = float(v) if (v := os.getenv("LLM_TEMPERATURE", "")) else None
LLM_TOP_P = float(v) if (v := os.getenv("LLM_TOP_P", "")) else None
LLM_SEED = int(v) if (v := os.getenv("LLM_SEED", "")) else None
LLM_PRESENCE_PENALTY = float(v) if (v := os.getenv("LLM_PRESENCE_PENALTY", "")) else None
LLM_FREQUENCY_PENALTY = float(v) if (v := os.getenv("LLM_FREQUENCY_PENALTY", "")) else None
WEB_SEARCH_API_KEY = os.getenv("WEB_SEARCH_API_KEY", "")
WEB_SEARCH_URL = os.getenv("WEB_SEARCH_URL", "")
DB_SEARCH_URL = os.getenv("DB_SEARCH2_URL", "")
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY", "")
PROMPT_FILE = os.getenv("SLIME_PROMPT_FILE", str(Path(__file__).parent / "slime_prompt.txt"))

MAX_ROUNDS = 10

# 调试日志默认开启（输出到 stderr，stdout 仍只有最终回答）。
# 关闭：--quiet 或 SLIME_DEBUG=0；
# verbose（额外打印 LLM 完整输入/输出）：--verbose 或 SLIME_VERBOSE=1；
# 改打到 stdout：--debug-stdout 或 SLIME_DEBUG_STDOUT=1。
def _env_flag(name: str, default: bool) -> bool:
    v = os.getenv(name, "")
    return default if v == "" else v.lower() in ("1", "true", "yes")


VERBOSE = _env_flag("SLIME_VERBOSE", False)
DEBUG = VERBOSE or _env_flag("SLIME_DEBUG", True)
DEBUG_STREAM = sys.stdout if _env_flag("SLIME_DEBUG_STDOUT", False) else sys.stderr
LOG_FILE = None   # 打开的日志文件句柄（在 main 中按需打开）
LOG_PATH = None   # 日志文件路径（用于配置打印展示）


def _out(prefix: str, body: str, console_body: str | None = None) -> None:
    """把一行日志同时输出到控制台（按 DEBUG 决定）与日志文件（若已开启）。

    console_body 可传入截断后的版本用于控制台显示；日志文件始终写完整 body。
    """
    cb = console_body if console_body is not None else body
    if DEBUG:
        sep = "\n" if "\n" in cb else " "
        print(f"\033[2m{prefix}\033[0m{sep}{cb}".rstrip(), file=DEBUG_STREAM, flush=True)
    if LOG_FILE:
        sep = "\n" if "\n" in body else " "
        LOG_FILE.write(f"{prefix}{sep}{body}".rstrip() + "\n")
        LOG_FILE.flush()


def _dbg(label: str, content: str = "", limit: int = 1200) -> None:
    if not (DEBUG or LOG_FILE):
        return
    disp = content
    if content and len(content) > limit:
        disp = content[:limit] + f"...（已截断，共 {len(content)} 字，完整内容见日志文件）"
    _out(f"[debug] {label}", content, disp)


def _vrb(label: str, content: str = "") -> None:
    """verbose 级别：打印 LLM 完整输入/原始输出，不截断。"""
    if not VERBOSE:
        return
    _out(f"[verbose] {label}", content)


def _pretty_json(s: str) -> str:
    """尝试把 JSON 字符串美化成多行；失败则原样返回。"""
    try:
        return json.dumps(json.loads(s), ensure_ascii=False, indent=2)
    except (json.JSONDecodeError, TypeError):
        return s


def _fmt_tool_call(name: str, arguments: str) -> str:
    return f"  • tool_call: {name}\n{_indent(_pretty_json(arguments))}"


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def _fmt_message(m: dict) -> str:
    """把单条 message 渲染成可读文本（真实换行，不转义）。"""
    role = m.get("role", "?")
    cid = m.get("tool_call_id")
    head = f"──── [{role}]" + (f" (id={cid})" if cid else "") + " ────"
    lines = [head]
    content = m.get("content")
    if content:
        # tool 消息内容多为 JSON 字符串，尝试美化
        lines.append(_pretty_json(content) if role == "tool" else str(content))
    for tc in (m.get("tool_calls") or []):
        fn = tc.get("function", {})
        lines.append(_fmt_tool_call(fn.get("name", ""), fn.get("arguments", "")))
    return "\n".join(lines)


def _format_messages(messages: list[dict]) -> str:
    return "\n\n".join(_fmt_message(m) for m in messages)


def _format_response(resp) -> str:
    """把 LLM 原始响应渲染成可读文本。"""
    choice = resp.choices[0]
    msg = choice.message
    parts = [f"finish_reason: {choice.finish_reason}"]
    reasoning = _extract_reasoning(msg)
    if reasoning:
        parts.append(f"──── reasoning ────\n{reasoning}")
    if msg.content:
        parts.append(f"──── content ────\n{msg.content}")
    for tc in (msg.tool_calls or []):
        parts.append(f"──── tool_call ────\n{_fmt_tool_call(tc.function.name, tc.function.arguments)}")
    u = getattr(resp, "usage", None)
    if u:
        parts.append(f"──── usage ────\nprompt={getattr(u, 'prompt_tokens', '?')} "
                     f"completion={getattr(u, 'completion_tokens', '?')} total={getattr(u, 'total_tokens', '?')}")
    return "\n".join(parts)


def _extract_reasoning(message) -> str | None:
    """提取思考链内容（不同厂商字段名不同，尽量兼容）。"""
    r = getattr(message, "reasoning_content", None) or getattr(message, "thinking_content", None)
    if not r and getattr(message, "model_extra", None):
        r = message.model_extra.get("reasoning_content") or message.model_extra.get("thinking_content")
    return r


# ============================================================
# 工具定义（与线上 app/tools.py 一致的 schema）
# ============================================================
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "搜索互联网获取影视相关信息。当需要查找电影、电视剧、综艺、动画等媒资的名称、演员、导演、剧情等信息时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词，应尽量精准描述要查找的影视内容"},
                    "count": {"type": "integer", "description": "返回结果数量，默认10，范围1-50"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "db_search",
            "description": (
                "在平台内部视频媒资库中按标题检索，确认媒资是否可用。支持按分类、评分、年份、标签等多维度过滤。"
                "返回结果中 is_same_match=1 的才是确认可用的媒资。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "titles": {"type": "array", "items": {"type": "string"}, "description": "要检索的媒资标题列表，支持批量查询，自动去重"},
                    "product": {"type": "string", "enum": ["vod", "educ"], "description": "媒资库类型：vod=一般媒资（默认），educ=少儿/教育媒资"},
                    "countries": {"type": "array", "items": {"type": "string"}, "description": "国家/地区白名单，如 [\"内地\", \"香港\"]，不传则不限"},
                    "categories": {"type": "array", "items": {"type": "string"}, "description": "主分类白名单：电影、电视剧、综艺、纪录片、动漫，不传则全部"},
                    "require_high_quality": {"type": "boolean", "description": "是否只返回高质量内容，默认 true；设为 false 可召回更多冷门内容"},
                    "is_fee": {"type": "integer", "enum": [0, 1], "description": "付费筛选：0=免费，1=付费，不传则不限"},
                    "min_rate": {"type": "number", "description": "平台评分下限（0-9.5），不传则不限"},
                    "min_douban_rate": {"type": "number", "description": "豆瓣评分下限，不传则不限"},
                    "pubdate_from": {"type": "string", "description": "发布年份下限，格式 YYYY"},
                    "pubdate_to": {"type": "string", "description": "发布年份上限，格式 YYYY"},
                    "tags_include": {"type": "array", "items": {"type": "string"}, "description": "tag 白名单（OR 语义），至少包含其中一个才通过"},
                    "tags_exclude": {"type": "array", "items": {"type": "string"}, "description": "tag 黑名单，包含任意一个即被过滤"},
                    "directors_include": {"type": "array", "items": {"type": "string"}, "description": "导演白名单（OR 语义），不传则不限"},
                    "actors_include": {"type": "array", "items": {"type": "string"}, "description": "演员白名单（OR 语义），与 directors_include 同传时为 AND，不传则不限"},
                },
                "required": ["titles"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询指定城市的当前天气信息。当用户提问涉及天气或需要根据天气推荐内容时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "城市名称，如 beijing、hangzhou，使用拼音"},
                },
                "required": ["location"],
            },
        },
    },
]


# ============================================================
# 工具执行
# ============================================================
def web_search(query: str, count: int = 10) -> dict:
    try:
        resp = httpx.get(
            WEB_SEARCH_URL,
            params={"q": query, "count": count},
            headers={"Authorization": f"Bearer {WEB_SEARCH_API_KEY}"},
            timeout=30.0,
        )
        resp.raise_for_status()
        pages = resp.json().get("webPages", {}).get("value", [])
        results = [
            {"name": p.get("name", ""), "url": p.get("url", ""), "snippet": p.get("snippet", ""), "score": p.get("score", 0)}
            for p in pages
        ]
        results.sort(key=lambda x: x.get("score", 0), reverse=True)
        return {"status": "success", "results": results}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def db_search(args: dict) -> dict:
    payload = {k: v for k, v in args.items() if v is not None}
    try:
        resp = httpx.post(DB_SEARCH_URL, json=payload, headers={"Content-Type": "application/json"}, timeout=30.0)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        return {"status": "error", "message": str(e)}


def get_weather(location: str) -> dict:
    try:
        resp = httpx.get(
            "https://api.seniverse.com/v3/weather/now.json",
            params={"key": WEATHER_API_KEY, "location": location, "language": "zh-Hans", "unit": "c"},
            timeout=10.0,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            return {"status": "error", "message": "未找到该城市天气信息"}
        now = results[0].get("now", {})
        return {"status": "success", "location": location, "weather": now.get("text", ""), "temperature": now.get("temperature", "")}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def execute_tool(name: str, args: dict) -> dict:
    if name == "web_search":
        if not args.get("query"):
            return {"status": "error", "message": "缺少必填参数 query"}
        return web_search(args["query"], args.get("count", 10))
    if name == "db_search":
        titles = args.get("titles")
        if not titles or not isinstance(titles, list) or not any(isinstance(t, str) and t.strip() for t in titles):
            return {"status": "error", "message": "db_search 缺少必填参数 titles：必须提供至少一个媒资标题；无具体片名时先用 web_search 获取候选。"}
        return db_search(args)
    if name == "get_weather":
        if not args.get("location"):
            return {"status": "error", "message": "缺少必填参数 location"}
        return get_weather(args["location"])
    return {"status": "error", "message": f"Unknown tool: {name}"}


# ============================================================
# Agent 循环
# ============================================================
def _mask(secret: str) -> str:
    """脱敏展示密钥：保留首尾各 4 位，中间打码；未设置则标注。"""
    if not secret:
        return "(未设置)"
    if len(secret) <= 8:
        return "****"
    return f"{secret[:4]}...{secret[-4:]}"


def _log_open(path) -> None:
    """打开日志文件（追加模式），写入会话分隔头。"""
    global LOG_FILE, LOG_PATH
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    LOG_FILE = open(p, "a", encoding="utf-8")
    LOG_PATH = str(p)
    LOG_FILE.write(f"\n========== 会话开始 {datetime.now().isoformat(timespec='seconds')} ==========\n")
    LOG_FILE.flush()


def _log_close() -> None:
    global LOG_FILE
    if LOG_FILE:
        LOG_FILE.write(f"========== 会话结束 {datetime.now().isoformat(timespec='seconds')} ==========\n")
        LOG_FILE.close()
        LOG_FILE = None


def _print_config() -> None:
    """运行开始时打印关键配置项（控制台 + 日志文件）。"""
    mode = "verbose" if VERBOSE else ("debug" if DEBUG else "normal")
    lines = [
        "==================== slime 配置 ====================",
        f"  LLM_MODEL        : {LLM_MODEL}",
        f"  LLM_API_URL      : {LLM_API_URL}",
        f"  LLM_API_KEY      : {_mask(LLM_API_KEY)}",
        f"  LLM_MAX_TOKENS   : {LLM_MAX_TOKENS}",
        f"  采样参数         : temperature={LLM_TEMPERATURE if LLM_TEMPERATURE is not None else '默认'}"
        f" top_p={LLM_TOP_P if LLM_TOP_P is not None else '默认'}"
        f" seed={LLM_SEED if LLM_SEED is not None else '默认'}",
        f"  WEB_SEARCH_URL   : {WEB_SEARCH_URL or '(未设置)'}",
        f"  WEB_SEARCH_KEY   : {_mask(WEB_SEARCH_API_KEY)}",
        f"  DB_SEARCH_URL    : {DB_SEARCH_URL or '(未设置)'}",
        f"  WEATHER_API_KEY  : {_mask(WEATHER_API_KEY)}",
        f"  PROMPT_FILE      : {PROMPT_FILE}" + ("" if Path(PROMPT_FILE).exists() else "  ⚠ 文件不存在"),
        f"  MAX_ROUNDS       : {MAX_ROUNDS}",
        f"  输出模式         : {mode}",
        f"  日志文件         : {LOG_PATH or '(未启用)'}",
        "====================================================",
    ]
    text = "\n".join(lines)
    if DEBUG:
        print(text, file=DEBUG_STREAM, flush=True)
    if LOG_FILE:
        LOG_FILE.write(text + "\n")
        LOG_FILE.flush()


def system_prompt() -> str:
    tpl = Path(PROMPT_FILE).read_text(encoding="utf-8")
    return tpl.replace("{current_date}", datetime.now().strftime("%Y年%m月%d日"))


def _create_kwargs() -> dict:
    """构建 chat.completions.create 的参数；采样参数仅在显式配置时才传。"""
    kwargs = dict(model=LLM_MODEL, tools=TOOLS, tool_choice="auto", max_tokens=LLM_MAX_TOKENS)
    if LLM_TEMPERATURE is not None:
        kwargs["temperature"] = LLM_TEMPERATURE
    if LLM_TOP_P is not None:
        kwargs["top_p"] = LLM_TOP_P
    if LLM_SEED is not None:
        kwargs["seed"] = LLM_SEED
    if LLM_PRESENCE_PENALTY is not None:
        kwargs["presence_penalty"] = LLM_PRESENCE_PENALTY
    if LLM_FREQUENCY_PENALTY is not None:
        kwargs["frequency_penalty"] = LLM_FREQUENCY_PENALTY
    return kwargs


def ask(query: str, trace: dict | None = None) -> str:
    """跑一轮完整 Agent 循环，返回最终文本回答。

    若传入 trace（dict），会就地填充结构化轨迹，便于 benchmark 等场景 dump：
        trace["rounds"]   = [{round, thinking, content, usage, tool_calls:[{name,args,result}]}]
        trace["messages"] = 完整 messages 列表（含 system/user/assistant/tool）
        trace["answer"]   = 最终回答
        trace["rounds_used"] = 实际使用轮数
    """
    rounds_trace: list[dict] = []
    if trace is not None:
        trace.setdefault("rounds", rounds_trace)
        rounds_trace = trace["rounds"]

    def _finish(answer: str, used: int) -> str:
        if trace is not None:
            trace["answer"] = answer
            trace["messages"] = messages
            trace["rounds_used"] = used
        return answer

    client = OpenAI(api_key=LLM_API_KEY, base_url=f"{LLM_API_URL}/v1")
    create_kwargs = _create_kwargs()
    messages = [
        {"role": "system", "content": system_prompt()},
        {"role": "user", "content": query},
    ]

    for round_idx in range(MAX_ROUNDS):
        _dbg(f"===== 第 {round_idx + 1} 轮 LLM 调用 =====")
        _vrb("LLM 输入 messages:", _format_messages(messages))
        resp = client.chat.completions.create(messages=messages, **create_kwargs)
        msg = resp.choices[0].message
        _vrb("LLM 原始输出:", _format_response(resp))

        reasoning = _extract_reasoning(msg)
        if reasoning:
            _dbg("thinking:", reasoning)
        if msg.content:
            _dbg("assistant:", msg.content)
        usage = getattr(resp, "usage", None)
        if usage:
            _dbg("usage:", f"prompt={getattr(usage, 'prompt_tokens', '?')} "
                           f"completion={getattr(usage, 'completion_tokens', '?')} "
                           f"total={getattr(usage, 'total_tokens', '?')}")

        round_rec = {
            "round": round_idx + 1,
            "thinking": reasoning or "",
            "content": msg.content or "",
            "usage": {
                "prompt": getattr(usage, "prompt_tokens", None),
                "completion": getattr(usage, "completion_tokens", None),
                "total": getattr(usage, "total_tokens", None),
            } if usage else None,
            "tool_calls": [],
        }
        rounds_trace.append(round_rec)

        # 没有工具调用 → 已是最终回答
        if not msg.tool_calls:
            _dbg(f"无工具调用，结束于第 {round_idx + 1} 轮")
            return _finish(msg.content or "", round_idx + 1)

        # 有工具调用 → 执行后回填结果，继续下一轮
        messages.append(msg.model_dump())
        _dbg(f"本轮工具调用数: {len(msg.tool_calls)}")
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments)
            except (json.JSONDecodeError, TypeError):
                args = {}
                _dbg("⚠ 工具参数 JSON 解析失败:", tc.function.arguments or "")
            _dbg("tool_call →", f"{tc.function.name}({json.dumps(args, ensure_ascii=False)})")
            result = execute_tool(tc.function.name, args)
            _dbg(f"tool_result ← {tc.function.name}:", json.dumps(result, ensure_ascii=False))
            round_rec["tool_calls"].append({"name": tc.function.name, "args": args, "result": result})
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result, ensure_ascii=False)})

    return _finish("（已达到最大工具调用轮次，未能给出最终回答）", MAX_ROUNDS)


def main():
    global DEBUG, VERBOSE, DEBUG_STREAM
    parser = argparse.ArgumentParser(description="极简离线版影视问答 Agent")
    parser.add_argument("query", nargs="*", help="影视问题；不传则交互式输入")
    parser.add_argument("--quiet", "-q", action="store_true", help="关闭调试日志，stderr 只在出错时输出")
    parser.add_argument("--debug", action="store_true", help="开启调试日志（默认已开启，此项为兼容保留）")
    parser.add_argument("--verbose", action="store_true", help="在 debug 基础上，额外打印每轮 LLM 的完整输入与原始输出")
    parser.add_argument("--debug-stdout", action="store_true", help="把调试/配置信息改打到 stdout（便于单个 > 重定向到文件）")
    parser.add_argument("--log-file", default=None, help="日志文件路径；默认 logs/slime_<时间戳>.log")
    parser.add_argument("--no-log", action="store_true", help="不写日志文件")
    args = parser.parse_args()
    if args.verbose:
        VERBOSE = True
    if args.quiet:
        DEBUG = VERBOSE = False
    elif args.debug or VERBOSE:
        DEBUG = True
    if args.debug_stdout:
        DEBUG_STREAM = sys.stdout

    # 日志文件：--no-log 关闭；否则用 --log-file / SLIME_LOG_FILE / 默认时间戳路径
    if not args.no_log:
        log_path = args.log_file or os.getenv("SLIME_LOG_FILE") or (
            Path(__file__).parent / "logs" / f"slime_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        )
        _log_open(log_path)

    try:
        _print_config()
        query = " ".join(args.query).strip() or input("请输入影视问题：").strip()
        if query:
            if LOG_FILE:
                LOG_FILE.write(f"[query] {query}\n")
            answer = ask(query)
            if LOG_FILE:
                LOG_FILE.write(f"[answer]\n{answer}\n")
            print("\n" + answer)
    finally:
        _log_close()


if __name__ == "__main__":
    main()
