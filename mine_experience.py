"""
从 benchmark 轨迹里挖"经验"——experience learning 的第一步（离线、无需联网）。

输入两次 run 的输出目录：
  --weak    表现较弱的 run（要从中挖失败模式，如 baseline）
  --strong  表现较好的 run（同题成功轨迹做对照，如 flash）

它会：
  1. 逐条重算命中（用 run_benchmark 的当前口径，保证一致）
  2. 检测每条失败轨迹的行为信号：撞轮次上限 / 重复相同工具调用 / db_search 报错未纠正 /
     命中后不收尾 / 疑似把剧情描述词当 titles / 轮次过多
  3. 对每个失败题，取 strong run 同题的成功轨迹做对照（紧凑工具序列）
  4. 汇总各失败模式的占比，产出一份可直接审阅的蒸馏稿 insights.md
     （把经验显式化为可粘进 slime_prompt.txt 的规则 + 若干对照实例）

用法：
  python mine_experience.py --weak runs/exp1 --strong runs/exp2
  python mine_experience.py --weak runs/exp3 --strong runs/exp2 --out experience/run1 --max-examples 4
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import slime
import run_benchmark as rb

CAP_SENTINEL = "已达到最大工具调用轮次"


# ============================================================
# 轨迹解析与信号检测
# ============================================================
def _titles(args: dict) -> list[str]:
    t = args.get("titles")
    return [str(x) for x in t] if isinstance(t, list) else []


def _titles_key(args: dict) -> tuple:
    return tuple(sorted(_titles(args))) or ("",)


def _db_has_hit(result: dict) -> bool:
    """db_search 是否查到库内候选（results 里任一标题对应非空列表）。"""
    if not isinstance(result, dict):
        return False
    res = result.get("results")
    if isinstance(res, dict):
        return any(isinstance(v, list) and len(v) > 0 for v in res.values())
    return False


def iter_traces(run_dir: Path):
    """逐行读取 traces.jsonl，yield 每条结果 dict。

    若目录下没有 traces/traces.jsonl（例如 --strong 指向了不存在或未 dump 轨迹
    的目录），打印告警并返回空，而不是抛 FileNotFoundError 中断整个流程。
    """
    p = Path(run_dir) / "traces" / "traces.jsonl"
    if not p.exists():
        print(f"⚠ 未找到轨迹文件，忽略该目录：{p}", file=sys.stderr)
        return
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def tool_sequence(trace: dict, max_titles: int = 3) -> str:
    """把工具调用渲染成紧凑序列，如 ws×2 → db[玫瑰的故事]✓ → answer。"""
    parts = []
    for rd in trace.get("rounds", []):
        ws = 0
        chunk = []
        for tc in rd.get("tool_calls") or []:
            if tc.get("name") == "web_search":
                ws += 1
            elif tc.get("name") == "db_search":
                a = tc.get("args") or {}
                ts = _titles(a)
                res = tc.get("result") or {}
                if res.get("status") == "error":
                    mark = "✗err"
                elif _db_has_hit(res):
                    mark = "✓"
                else:
                    mark = "∅"
                shown = "/".join(ts[:max_titles]) + ("…" if len(ts) > max_titles else "")
                chunk.append(f"db[{shown or 'None'}]{mark}")
        if ws:
            chunk.insert(0, f"ws×{ws}")
        if chunk:
            parts.append(" ".join(chunk))
    return "  →  ".join(parts) if parts else "(无工具调用)"


def detect_signals(rec: dict) -> dict:
    """检测单条轨迹的失败行为信号，返回 {signal: bool/int}。"""
    trace = rec.get("trace") or {}
    rounds = trace.get("rounds", [])
    answer = rec.get("answer", "") or ""

    wq = Counter()
    dbk = Counter()
    db_err = 0
    db_calls = 0
    matched_round = None
    plot_titles_calls = 0

    for ri, rd in enumerate(rounds):
        for tc in rd.get("tool_calls") or []:
            name = tc.get("name")
            a = tc.get("args") or {}
            res = tc.get("result") or {}
            if name == "web_search":
                wq[a.get("query", "")] += 1
            elif name == "db_search":
                db_calls += 1
                dbk[_titles_key(a)] += 1
                if res.get("status") == "error":
                    db_err += 1
                if _db_has_hit(res) and matched_round is None:
                    matched_round = ri
                # 疑似把"剧情描述词"当标题：标题偏长且该次全 0 命中
                ts = _titles(a)
                if ts and not _db_has_hit(res):
                    if sum(len(t) for t in ts) / len(ts) >= 5:
                        plot_titles_calls += 1

    ws_dup = sum(c - 1 for c in wq.values() if c > 1)
    db_dup = sum(c - 1 for c in dbk.values() if c > 1)
    rounds_used = trace.get("rounds_used", len(rounds))
    cap_hit = (CAP_SENTINEL in answer) or (rounds_used >= slime.MAX_ROUNDS)
    # 命中后不收尾：出现库内命中，但其后仍有 >=2 轮工具调用 / 或最终撞上限
    no_stop_after_match = bool(
        matched_round is not None and (cap_hit or (len(rounds) - matched_round - 1) >= 2)
    )

    return {
        "rounds": rounds_used,
        "cap_hit": cap_hit,
        "repeat_db": db_dup,           # db_search 同 titles 重复次数
        "repeat_web": ws_dup,          # web_search 同 query 重复次数
        "db_error": db_err,            # db_search 报错次数（多为缺 titles）
        "plot_as_titles": plot_titles_calls,
        "no_stop_after_match": no_stop_after_match,
        "many_rounds": rounds_used >= 6,
    }


# 失败模式 → 规则草稿（出现才纳入）
RULE_TEMPLATES = {
    "repeat_call": (
        "重复检索熔断：若本轮要发起的 web_search query 或 db_search titles 与之前任意一轮完全相同，"
        "禁止重发。相同语义没有新信息时，应改变检索维度或直接给出结论。"
    ),
    "db_error": (
        "工具报错必须纠正：db_search 报\"缺少必填参数 titles\"时，不要再用空 titles 重试；"
        "先用 web_search 提取确切候选片名，再带 titles 调用。连续 2 次同类报错则停止调用、转文字作答。"
    ),
    "no_stop_after_match": (
        "命中即收尾：一旦 db_search 返回库内命中（is_same_match=1 / results 非空），"
        "立即据此组织最终回答与推荐卡片，不要再重复检索同一标题。"
    ),
    "plot_as_titles": (
        "titles 只放片名：db_search 的 titles 必须是候选\"作品标题\"，不能放剧情描述词/演员/题材短语。"
        "当用户只给剧情线索时，先 web_search 定位真实片名，再把片名放进 titles。"
    ),
    "many_rounds": (
        "控制轮次：前 1-2 轮并行多角度 web_search 收集候选，随后立即转 db_search 验证，"
        "总轮次尽量 ≤5；信息已足够时不要继续无谓检索。"
    ),
}

# 信号 → 规则键
SIGNAL_TO_RULE = {
    "repeat_db": "repeat_call",
    "repeat_web": "repeat_call",
    "db_error": "db_error",
    "no_stop_after_match": "no_stop_after_match",
    "plot_as_titles": "plot_as_titles",
    "many_rounds": "many_rounds",
}


def is_failure(rec: dict) -> bool:
    rank = rb.match_rank(rec.get("answer", ""), rec["label"])
    return not (1 <= rank <= 1)   # 以 hit@1 为失败判据


def main() -> None:
    ap = argparse.ArgumentParser(description="从 benchmark 轨迹挖失败模式并蒸馏经验")
    ap.add_argument("--weak", required=True, help="较弱 run 的输出目录（从中挖失败）")
    ap.add_argument("--strong", default=None, help="较好 run 的输出目录（同题成功对照）")
    ap.add_argument("--out", default="experience", help="输出目录")
    ap.add_argument("--max-examples", type=int, default=3, help="每个失败模式展示的对照实例数")
    args = ap.parse_args()

    weak_dir, out_dir = Path(args.weak), Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 读 strong run 建索引（idx -> rec）
    strong = {}
    if args.strong:
        for rec in iter_traces(Path(args.strong)):
            strong[rec["idx"]] = rec

    # 扫描 weak run
    total = 0
    failures = []
    sig_count = Counter()       # 失败题里各信号出现的题数
    rule_examples = defaultdict(list)
    with open(out_dir / "failures.jsonl", "w", encoding="utf-8") as fout:
        for rec in iter_traces(weak_dir):
            total += 1
            if not is_failure(rec):
                continue
            sig = detect_signals(rec)
            # 命中的信号（布尔为真或计数>0）
            hit_sigs = [k for k, v in sig.items()
                        if k in SIGNAL_TO_RULE and (v is True or (isinstance(v, int) and v > 0))]
            srec = strong.get(rec["idx"])
            strong_ok = bool(srec) and (1 <= rb.match_rank(srec.get("answer", ""), srec["label"]) <= 1)
            item = {
                "idx": rec["idx"],
                "query": rec["query"],
                "label": rec["label"],
                "signals": {k: sig[k] for k in sig},
                "weak_seq": tool_sequence(rec.get("trace") or {}),
                "strong_ok": strong_ok,
                "strong_seq": tool_sequence((srec or {}).get("trace") or {}) if srec else None,
                "strong_rounds": (srec or {}).get("trace", {}).get("rounds_used") if srec else None,
            }
            failures.append(item)
            fout.write(json.dumps(item, ensure_ascii=False) + "\n")
            for s in hit_sigs:
                sig_count[s] += 1
                rule = SIGNAL_TO_RULE[s]
                # 优先收集"weak失败、strong同题成功"的强对照
                if strong_ok and len(rule_examples[rule]) < args.max_examples:
                    rule_examples[rule].append(item)

    # 兜底：某规则没有 strong 对照实例时，用纯 weak 失败实例补
    for s in sig_count:
        rule = SIGNAL_TO_RULE[s]
        if len(rule_examples[rule]) < args.max_examples:
            for it in failures:
                if it["signals"].get(s) and it not in rule_examples[rule]:
                    rule_examples[rule].append(it)
                    if len(rule_examples[rule]) >= args.max_examples:
                        break

    # 输出 insights.md
    n_fail = len(failures)
    lines = [
        f"# 经验蒸馏稿（weak={args.weak} vs strong={args.strong}）",
        "",
        f"- 总题数：{total}　失败题(hit@1)：{n_fail}",
        "",
        "## 失败模式占比（失败题中命中该信号的题数）",
        "",
        "| 信号 | 题数 | 占失败 |",
        "|---|---|---|",
    ]
    label_cn = {
        "repeat_db": "db 重复相同 titles", "repeat_web": "web 重复相同 query",
        "db_error": "db 报错未纠正", "no_stop_after_match": "命中后不收尾",
        "plot_as_titles": "剧情词当 titles", "many_rounds": "轮次≥6",
    }
    for s, c in sig_count.most_common():
        lines.append(f"| {label_cn.get(s, s)} | {c} | {c / max(n_fail,1):.0%} |")

    lines += ["", "## 建议加入 slime_prompt.txt 的规则（审阅后粘贴）", ""]
    seen_rule = set()
    # 按相关信号题数排序输出规则
    rule_score = Counter()
    for s, c in sig_count.items():
        rule_score[SIGNAL_TO_RULE[s]] += c
    for rule, _ in rule_score.most_common():
        if rule in seen_rule:
            continue
        seen_rule.add(rule)
        lines.append(f"- {RULE_TEMPLATES[rule]}")
    lines += ["", "## 对照实例（weak 失败轨迹 vs strong 成功轨迹）", ""]
    for rule, _ in rule_score.most_common():
        lines.append(f"### 触发规则：{RULE_TEMPLATES[rule][:24]}…")
        for it in rule_examples.get(rule, []):
            lines.append(f"- #{it['idx']} [{it['label'].split('/')[0]}] {it['query'][:30]}")
            lines.append(f"  - weak  ({it['signals']['rounds']}轮): {it['weak_seq']}")
            if it["strong_seq"]:
                tag = "✓成功" if it["strong_ok"] else "✗也失败"
                lines.append(f"  - strong({it['strong_rounds']}轮,{tag}): {it['strong_seq']}")
        lines.append("")

    (out_dir / "insights.md").write_text("\n".join(lines), encoding="utf-8")

    print(f"扫描 {total} 题，失败 {n_fail} 题")
    print("失败模式：", ", ".join(f"{label_cn.get(s,s)}={c}" for s, c in sig_count.most_common()))
    print(f"产物：{out_dir}/failures.jsonl, {out_dir}/insights.md")


if __name__ == "__main__":
    main()
