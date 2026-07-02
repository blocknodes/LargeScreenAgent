"""
slime benchmark 跑分脚本。

读取 benchmark CSV（每行 `query,label`，label 可用 ` / ` 分隔多个可接受答案），
对每条 query 调用 slime 的 Agent 循环（slime.ask）。

本次运行的所有产物都集中写入一个输出目录（-d/--out-dir，默认 benchmark_out/run_<时间戳>）：
  - config.json   本次跑的配置（模型/采样参数/接口地址 + 相关环境变量，密钥脱敏）
  - result.csv    逐条明细（含 ok / rank / hit@1/3/5/all / 轮次 / token）
  - summary.json  汇总指标
  - traces/       每条 query 的详细轨迹：{idx}.txt（可读）+ traces.jsonl（结构化）

判定指标（标题归一化：中文数字转阿拉伯、剥离序数词 第/季/部/集/篇/章、去符号空格）：
  - ok        ：label 任一别名（归一化）作为子串出现在回答任意位置（宽松，文本级）
  - rank      ：label 在最终回答【推荐媒资卡片】排序列表中的首个命中名次（1 起；0=未命中）；
               命中口径容忍"加戏"——卡片标题归一化后**包含**任一别名即可（如"片名（2025/演员）"也算对）
  - hit@1/3/5 ：rank 落在前 1/3/5 名即记一次命中
  - hit@all   ：label 命中卡片列表中任意名次（= rank>=1）
另输出每条/平均的轮次与 token 用量（prompt/completion/total/peak_prompt）。

支持并发。中间过程默认安静（关闭 slime 的逐轮调试日志），只在 stderr 打印进度。

用法：
    python run_benchmark.py                          # 跑默认 benchmark_0601.csv，并发 4
    python run_benchmark.py -c 8                      # 并发 8
    python run_benchmark.py benchmark_0601.csv -c 8   # 指定 CSV
    python run_benchmark.py -n 20                     # 只跑前 20 条（快速验证）
    python run_benchmark.py --shuffle -n 20           # 打乱后随机抽 20 条
    python run_benchmark.py --shuffle --seed 42       # 固定随机种子，结果可复现
    python run_benchmark.py -d runs/exp1              # 所有产物写入 runs/exp1/
    python run_benchmark.py --no-dump                 # 不写 traces/（仍写 config/result/summary）
"""

import argparse
import csv
import json
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import slime


def load_cases(csv_path: Path, limit: int | None = None) -> list[dict]:
    """读取 benchmark CSV，返回 [{idx, query, label}]。

    兼容带 BOM、带表头、以及电子表格导出的大量空尾列。
    自动跳过首行表头（query,label,...）和空行。
    """
    cases: list[dict] = []
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            query = (row[0] if len(row) > 0 else "").strip()
            label = (row[1] if len(row) > 1 else "").strip()
            if not query:
                continue
            # 跳过表头行
            if query.lower() == "query" and label.lower() == "label":
                continue
            if not label:
                continue
            cases.append({"idx": len(cases) + 1, "query": query, "label": label})
            if limit is not None and len(cases) >= limit:
                break
    return cases


def label_aliases(label: str) -> list[str]:
    """把 label 拆成多个可接受别名（按 / 分隔），去空白与空项。"""
    return [a.strip() for a in label.split("/") if a.strip()]


# ============================================================
# 标题归一化与命中判定
# 移植自 agent_daping_v5/benchmark/metrics.py，使两套打分口径一致：
# 中文数字转阿拉伯、剥离序数词、去符号后做精确相等匹配。
# ============================================================
_CN_NUM = {
    "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
    "十": 10, "百": 100, "千": 1000,
    "壹": 1, "贰": 2, "叁": 3, "肆": 4, "伍": 5,
    "陆": 6, "柒": 7, "捌": 8, "玖": 9, "拾": 10, "两": 2,
}


def _cn_to_int(cn: str) -> str:
    """中文数字串转阿拉伯数字串，如 '十七'->'17'，'二十三'->'23'；无法解析则原样返回。"""
    if not cn:
        return ""
    try:
        result, current = 0, 0
        for ch in cn:
            val = _CN_NUM.get(ch)
            if val is None:
                return cn
            if val >= 10:
                current = current or 1
                result += current * val
                current = 0
            else:
                current = val
        result += current
        return str(result) if result > 0 else cn
    except Exception:  # noqa: BLE001
        return cn


def _replace_cn_numbers(text: str) -> str:
    """把文本中连续的中文数字替换为阿拉伯数字。"""
    chars = set(_CN_NUM)
    out, i = [], 0
    while i < len(text):
        if text[i] in chars:
            j = i
            while j < len(text) and text[j] in chars:
                j += 1
            out.append(_cn_to_int(text[i:j]))
            i = j
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def normalize_title(title: str) -> str:
    """归一化标题：小写 → 中文数字转阿拉伯 → 去序数词(第/季/部/集/篇/章) → 仅留中英数字。"""
    s = title.lower()
    s = _replace_cn_numbers(s)
    s = re.sub(r"[第季部集篇章]", "", s)
    s = re.sub(r"[^\u4e00-\u9fff\w\d]", "", s)
    return s.replace("_", "")


def judge(answer: str, label: str) -> bool:
    """宽松命中：label 任一别名归一化后作为子串出现在归一化后的回答中。"""
    if not answer:
        return False
    na = normalize_title(answer)
    return any((nl := normalize_title(alias)) and nl in na for alias in label_aliases(label))


_CARD_HEADER = "推荐媒资卡片"
# 形如 "1. 标题" / "1、标题" / "1) 标题" / "1．标题"
_ITEM_RE = re.compile(r"^\s*\d+\s*[.、)．:：]\s*(.+?)\s*$")


def parse_card(answer: str) -> list[str]:
    """从最终回答中解析【推荐媒资卡片】里的有序媒资标题列表；无卡片则返回空列表。"""
    if not answer:
        return []
    lines = answer.splitlines()
    # 找到卡片标题行的位置，从其后开始收集编号条目
    start = next((i for i, ln in enumerate(lines) if _CARD_HEADER in ln), None)
    scan = lines[start + 1:] if start is not None else lines
    items: list[str] = []
    for ln in scan:
        m = _ITEM_RE.match(ln)
        if m:
            items.append(m.group(1).strip())
        elif items:
            # 卡片是连续编号块，遇到非编号行即结束
            break
    return items


def match_rank(answer: str, label: str) -> int:
    """返回 label 在卡片有序列表中的首个命中名次（1 起）；未命中返回 0。

    命中口径（容忍"加戏"）：卡片标题归一化后，只要**包含** label 的任一归一化别名即算命中。
    这样 base 把标题写成"片名（年份/演员/评分）"也能正确判对（归一化已去括号/标点，
    标题作为前缀/子串仍可匹配），不要求严格相等。
    """
    norm_labels = [n for a in label_aliases(label) if (n := normalize_title(a))]
    for i, item in enumerate(parse_card(answer), start=1):
        ni = normalize_title(item)
        if any(nl in ni for nl in norm_labels):
            return i
    return 0


def render_trace(r: dict) -> str:
    """把单条结果的轨迹渲染成可读文本。"""
    trace = r.get("trace") or {}
    lines = [
        "=" * 60,
        f"#{r['idx']}  query: {r['query']}",
        f"label : {r['label']}",
        f"ok={r['ok']}  rank={r['rank']}  "
        f"hit@1={int(r['hit1'])} hit@3={int(r['hit3'])} hit@5={int(r['hit5'])} hit@all={int(r['hit_all'])}  "
        f"rounds={trace.get('rounds_used', '?')}  tokens(total={r.get('total_tokens', 0)},"
        f"peak_prompt={r.get('peak_prompt_tokens', 0)})  "
        f"elapsed={r['elapsed']:.1f}s (llm={r.get('llm_ms', 0)/1000:.1f}s tool={r.get('tool_ms', 0)/1000:.1f}s)",
    ]
    if r["error"]:
        lines.append(f"error : {r['error']}")
    for rd in trace.get("rounds", []):
        _lm = rd.get("llm_ms")
        lines.append(f"\n---- round {rd['round']} ----" + (f"  (llm {_lm/1000:.1f}s)" if _lm else ""))
        if rd.get("thinking"):
            lines.append("[thinking]\n" + rd["thinking"])
        if rd.get("content"):
            lines.append("[assistant]\n" + rd["content"])
        for tc in rd.get("tool_calls", []):
            _tm = tc.get("ms")
            suffix = f"  ({_tm/1000:.1f}s)" if _tm else ""
            lines.append(f"[tool_call] {tc['name']}({json.dumps(tc['args'], ensure_ascii=False)}){suffix}")
            lines.append("[tool_result] " + json.dumps(tc["result"], ensure_ascii=False))
    lines.append("\n---- final answer ----")
    lines.append(r["answer"] or "(空)")
    return "\n".join(lines) + "\n"


# dump 目录（在 main 中设置；为 None 表示不 dump 轨迹）
DUMP_DIR: Path | None = None
# 经验库文件路径（在 main 中设置；为 None 表示不注入经验）
EXPERIENCE_FILE: str | None = None


def _env_snapshot() -> dict:
    """收集与本次运行相关的环境变量，密钥类自动脱敏。"""
    prefixes = ("LLM_", "WEB_SEARCH", "DB_SEARCH", "WEATHER", "SLIME_")
    secret_hint = ("KEY", "TOKEN", "SECRET", "PASSWORD")
    snap = {}
    for k in sorted(os.environ):
        if not k.startswith(prefixes):
            continue
        v = os.environ[k]
        snap[k] = slime._mask(v) if any(h in k.upper() for h in secret_hint) else v
    return snap


def collect_config(args, csv_path: Path, total: int) -> dict:
    """汇总本次跑的配置（含脱敏后的密钥与环境变量），用于写入 config.json。"""
    return {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "benchmark": {
            "csv": str(csv_path),
            "total_cases": total,
            "concurrency": args.concurrency,
            "limit": args.limit,
            "shuffle": args.shuffle,
            "seed": args.seed,
        },
        "llm": {
            "model": slime.LLM_MODEL,
            "api_url": slime.LLM_API_URL,
            "api_key": slime._mask(slime.LLM_API_KEY),
            "max_tokens": slime.LLM_MAX_TOKENS,
            "temperature": slime.LLM_TEMPERATURE,
            "top_p": slime.LLM_TOP_P,
            "seed": slime.LLM_SEED,
            "presence_penalty": slime.LLM_PRESENCE_PENALTY,
            "frequency_penalty": slime.LLM_FREQUENCY_PENALTY,
        },
        "tools": {
            "web_search_url": slime.WEB_SEARCH_URL,
            "web_search_key": slime._mask(slime.WEB_SEARCH_API_KEY),
            "db_search_url": slime.DB_SEARCH_URL,
            "weather_key": slime._mask(slime.WEATHER_API_KEY),
        },
        "prompt_file": slime.PROMPT_FILE,
        "experience_file": EXPERIENCE_FILE or "",
        "max_rounds": slime.MAX_ROUNDS,
        "env": _env_snapshot(),
    }


_print_lock = threading.Lock()


def _agg_tokens(trace: dict) -> dict:
    """从 trace 的各轮 usage 聚合 token 用量。

    prompt 每轮随上下文增长，故另记 peak（单轮最大 prompt）。
    """
    prompt = completion = total = peak_prompt = 0
    llm_ms = tool_ms = 0
    for rd in trace.get("rounds", []):
        u = rd.get("usage") or {}
        p, c, t = (u.get("prompt") or 0), (u.get("completion") or 0), (u.get("total") or 0)
        prompt += p
        completion += c
        total += t
        peak_prompt = max(peak_prompt, p)
        llm_ms += rd.get("llm_ms") or 0
        for tc in rd.get("tool_calls", []):
            tool_ms += tc.get("ms") or 0
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "peak_prompt_tokens": peak_prompt,
        "llm_ms": llm_ms,
        "tool_ms": tool_ms,
        "rounds": trace.get("rounds_used", len(trace.get("rounds", []))),
    }


def run_one(case: dict) -> dict:
    """跑单条用例，返回带结果的 dict。异常不抛出，记录到 error 字段。"""
    start = time.time()
    answer, error = "", ""
    trace: dict = {}
    try:
        answer = slime.ask(case["query"], trace=trace, experience_file=EXPERIENCE_FILE)
    except Exception as e:  # noqa: BLE001 - 跑分时单条失败不应中断整体
        error = f"{type(e).__name__}: {e}"
    elapsed = time.time() - start

    ok = judge(answer, case["label"]) if not error else False
    rank = match_rank(answer, case["label"]) if not error else 0
    tok = _agg_tokens(trace)
    result = {
        **case,
        "answer": answer,
        "ok": ok,
        "rank": rank,
        "hit1": 1 <= rank <= 1,
        "hit3": 1 <= rank <= 3,
        "hit5": 1 <= rank <= 5,
        "hit_all": rank >= 1,
        "error": error,
        "elapsed": elapsed,
        **tok,
        "trace": trace,
    }

    # 各线程写各自的轨迹文件，互不冲突
    if DUMP_DIR is not None:
        (DUMP_DIR / f"{case['idx']:04d}.txt").write_text(render_trace(result), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="slime benchmark 跑分脚本（支持并发）")
    parser.add_argument("csv", nargs="?", default="benchmark_0601.csv", help="benchmark CSV 路径")
    parser.add_argument("-c", "--concurrency", type=int, default=4, help="并发数（默认 4）")
    parser.add_argument("-n", "--limit", type=int, default=None, help="只跑前 N 条（默认全部）")
    parser.add_argument("--shuffle", action="store_true", help="打乱用例顺序（配合 -n 即为随机抽样）")
    parser.add_argument("--seed", type=int, default=None, help="shuffle 随机种子，固定后结果可复现")
    parser.add_argument("-d", "--out-dir", default=None,
                        help="本次运行的输出目录（默认 benchmark_out/run_<时间戳>）；"
                             "config.json / result.csv / summary.json / traces/ 都写入此处")
    parser.add_argument("--no-dump", action="store_true",
                        help="不写 traces/ 轨迹（仍写 config.json / result.csv / summary.json）")
    parser.add_argument("--experience", default=None,
                        help="经验库文件路径（注入 system prompt；也可用 SLIME_EXPERIENCE_FILE 环境变量）")
    args = parser.parse_args()

    global DUMP_DIR, EXPERIENCE_FILE

    # 关闭 slime 的逐轮调试日志与日志文件，避免并发下 stderr/日志互相干扰
    slime.DEBUG = False
    slime.VERBOSE = False
    slime.LOG_FILE = None

    # 经验库：命令行参数优先，其次环境变量
    EXPERIENCE_FILE = args.experience or os.getenv("SLIME_EXPERIENCE_FILE") or None

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"找不到 benchmark 文件：{csv_path}", file=sys.stderr)
        sys.exit(1)

    cases = load_cases(csv_path)
    if not cases:
        print("没有读到任何用例。", file=sys.stderr)
        sys.exit(1)

    # 先打乱（可固定种子），再按 limit 截断 —— 这样 --shuffle -n 即随机抽样
    if args.shuffle:
        rng = random.Random(args.seed)
        rng.shuffle(cases)
    if args.limit is not None:
        cases = cases[:args.limit]

    total = len(cases)
    shuffle_note = f"，shuffle(seed={args.seed})" if args.shuffle else ""

    # 本次运行的统一输出目录
    out_dir = Path(args.out_dir) if args.out_dir else (
        Path("benchmark_out") / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    if not args.no_dump:
        DUMP_DIR = out_dir / "traces"
        DUMP_DIR.mkdir(parents=True, exist_ok=True)

    # 先把配置（含脱敏环境变量）写入 config.json，崩溃也有记录
    config = collect_config(args, csv_path, total)
    (out_dir / "config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"输出目录：{out_dir}", file=sys.stderr)
    exp_note = f"，经验={EXPERIENCE_FILE}" if EXPERIENCE_FILE else ""
    print(f"加载 {total} 条用例，模型={slime.LLM_MODEL}，并发={args.concurrency}{shuffle_note}{exp_note}", file=sys.stderr)

    results: list[dict] = []
    done = 0
    start = time.time()
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futures = {pool.submit(run_one, c): c for c in cases}
        for fut in as_completed(futures):
            r = fut.result()
            results.append(r)
            done += 1
            mark = "✓" if r["ok"] else ("✗ ERROR" if r["error"] else "✗")
            with _print_lock:
                detail = r["error"] or r["answer"].replace("\n", " ")[:60]
                print(
                    f"[{done}/{total}] {mark} rank={r['rank']} ({r['elapsed']:.1f}s) "
                    f"label={r['label'].split('/')[0].strip()} | {r['query'][:30]} -> {detail}",
                    file=sys.stderr,
                    flush=True,
                )

    wall = time.time() - start
    results.sort(key=lambda x: x["idx"])

    correct = sum(1 for r in results if r["ok"])
    errors = sum(1 for r in results if r["error"])
    hit1 = sum(1 for r in results if r["hit1"])
    hit3 = sum(1 for r in results if r["hit3"])
    hit5 = sum(1 for r in results if r["hit5"])
    hit_all = sum(1 for r in results if r["hit_all"])
    acc = correct / total if total else 0.0

    # token / 耗时聚合（仅统计非异常用例）
    ok_runs = [r for r in results if not r["error"]]
    n = len(ok_runs) or 1
    avg_rounds = sum(r["rounds"] for r in ok_runs) / n
    avg_prompt = sum(r["prompt_tokens"] for r in ok_runs) / n
    avg_completion = sum(r["completion_tokens"] for r in ok_runs) / n
    avg_total_tok = sum(r["total_tokens"] for r in ok_runs) / n
    avg_peak_prompt = sum(r["peak_prompt_tokens"] for r in ok_runs) / n
    max_peak_prompt = max((r["peak_prompt_tokens"] for r in ok_runs), default=0)
    max_total_tok = max((r["total_tokens"] for r in ok_runs), default=0)
    avg_elapsed = sum(r["elapsed"] for r in ok_runs) / n
    avg_llm_s = sum(r.get("llm_ms", 0) for r in ok_runs) / n / 1000
    avg_tool_s = sum(r.get("tool_ms", 0) for r in ok_runs) / n / 1000

    def pct(x: int) -> str:
        return f"{x}/{total} ({x / total:.2%})" if total else "0/0"

    # 按 label 分组统计
    by_label: dict[str, list[bool]] = {}
    for r in results:
        by_label.setdefault(r["label"], []).append(r["ok"])

    print("\n==================== 跑分结果 ====================", file=sys.stderr)
    print(f"  总用例     : {total}", file=sys.stderr)
    print(f"  ok 命中    : {pct(correct)}", file=sys.stderr)
    print(f"  hit@1      : {pct(hit1)}", file=sys.stderr)
    print(f"  hit@3      : {pct(hit3)}", file=sys.stderr)
    print(f"  hit@5      : {pct(hit5)}", file=sys.stderr)
    print(f"  hit@all    : {pct(hit_all)}", file=sys.stderr)
    print(f"  异常       : {errors}", file=sys.stderr)
    print(f"  总耗时     : {wall:.1f}s（并发 {args.concurrency}），单条均耗时 {avg_elapsed:.1f}s"
          f"（llm {avg_llm_s:.1f}s + tool {avg_tool_s:.1f}s）", file=sys.stderr)
    print(f"  平均轮次   : {avg_rounds:.1f}", file=sys.stderr)
    print(f"  token/条   : prompt={avg_prompt:,.0f} completion={avg_completion:,.0f} "
          f"total={avg_total_tok:,.0f}（peak_prompt 均 {avg_peak_prompt:,.0f}/最大 {max_peak_prompt:,}）",
          file=sys.stderr)
    print("  ---- 按 label 分组（仅列出有错的）----", file=sys.stderr)
    for label, oks in sorted(by_label.items(), key=lambda kv: sum(kv[1]) / len(kv[1])):
        c, m = sum(oks), len(oks)
        if c < m:
            print(f"    {c}/{m}  {label.split('/')[0].strip()}", file=sys.stderr)
    print("==================================================", file=sys.stderr)

    # 逐条明细 result.csv（始终写入输出目录）
    result_csv = out_dir / "result.csv"
    with open(result_csv, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["idx", "query", "label", "ok", "rank",
                         "hit1", "hit3", "hit5", "hit_all",
                         "rounds", "prompt_tokens", "completion_tokens", "total_tokens",
                         "peak_prompt_tokens", "error", "elapsed_s", "llm_s", "tool_s", "answer"])
        for r in results:
            writer.writerow([
                r["idx"], r["query"], r["label"], int(r["ok"]), r["rank"],
                int(r["hit1"]), int(r["hit3"]), int(r["hit5"]), int(r["hit_all"]),
                r["rounds"], r["prompt_tokens"], r["completion_tokens"], r["total_tokens"],
                r["peak_prompt_tokens"], r["error"], f"{r['elapsed']:.1f}",
                f"{r.get('llm_ms', 0) / 1000:.1f}", f"{r.get('tool_ms', 0) / 1000:.1f}", r["answer"],
            ])

    # 结构化轨迹 jsonl（含完整 messages / 每轮 tool 调用）
    if DUMP_DIR is not None:
        with open(DUMP_DIR / "traces.jsonl", "w", encoding="utf-8") as f:
            for r in results:
                f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

    # 汇总指标 summary.json
    summary = {
        "total": total,
        "ok": correct,
        "hit@1": hit1,
        "hit@3": hit3,
        "hit@5": hit5,
        "hit@all": hit_all,
        "errors": errors,
        "ok_rate": round(acc, 4),
        "hit@1_rate": round(hit1 / total, 4) if total else 0.0,
        "hit@3_rate": round(hit3 / total, 4) if total else 0.0,
        "hit@5_rate": round(hit5 / total, 4) if total else 0.0,
        "hit@all_rate": round(hit_all / total, 4) if total else 0.0,
        "wall_seconds": round(wall, 1),
        "concurrency": args.concurrency,
        "avg_elapsed": round(avg_elapsed, 2),
        "avg_llm_s": round(avg_llm_s, 2),
        "avg_tool_s": round(avg_tool_s, 2),
        "avg_rounds": round(avg_rounds, 1),
        "avg_prompt_tokens": round(avg_prompt),
        "avg_completion_tokens": round(avg_completion),
        "avg_total_tokens": round(avg_total_tok),
        "avg_peak_prompt_tokens": round(avg_peak_prompt),
        "max_peak_prompt_tokens": max_peak_prompt,
        "max_total_tokens": max_total_tok,
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n产物已写入 {out_dir}/ ：config.json, result.csv, summary.json"
          + ("，traces/" if DUMP_DIR is not None else ""), file=sys.stderr)

    # 关键指标打到 stdout，便于脚本/管道采集
    print(f"ok={acc:.4f} hit@1={hit1 / total:.4f} hit@3={hit3 / total:.4f} "
          f"hit@5={hit5 / total:.4f} hit@all={hit_all / total:.4f}"
          if total else "no cases")


if __name__ == "__main__":
    main()
