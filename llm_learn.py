"""
用强模型（LLM）从 benchmark 轨迹中总结经验——experience learning 的智能版。

与 learn_from_run.py（规则模板匹配）不同，本脚本把失败案例的完整轨迹喂给一个
"牛逼的模型"，让它扮演 Agent 行为分析专家，自己归纳出可泛化、可执行的经验规则。

流程：
  1. 读取 benchmark 的 traces.jsonl
  2. 筛选失败案例（hit@1 未命中）；若提供 --strong 则附上同题成功轨迹做对照
  3. 把案例渲染成紧凑文本，分批构造 prompt 交给强模型
  4. 模型输出结构化 JSON 经验规则
  5. 解析后去重，写入经验库 experience/rules.jsonl（复用 ExperienceStore）

用法：
  # 用默认模型总结（读 .env 的 LLM_MODEL）
  python llm_learn.py --weak runs/baseline --strong runs/exp2

  # 指定一个更强的模型来做总结
  python llm_learn.py --weak runs/baseline --model deepseek-r1 --store experience/llm.jsonl

  # 总结模型走另一套服务商/凭证（不影响跑分用的 LLM_*）：
  #   方式一：命令行参数
  python llm_learn.py --weak runs/baseline --model deepseek-r1 \
      --api-key sk-xxx --api-url https://api.deepseek.com
  #   方式二：环境变量（.env 或 shell）
  #   LEARN_API_KEY / LEARN_API_URL / LEARN_MODEL，未设则回退到 LLM_*
  LEARN_API_KEY=sk-xxx LEARN_API_URL=https://api.deepseek.com LEARN_MODEL=deepseek-r1 \
      python llm_learn.py --weak runs/baseline

  # 预览将发送给模型的 prompt，不实际调用
  python llm_learn.py --weak runs/baseline --dry-run

  # 控制规模：最多取 40 条失败案例，每批 20 条
  python llm_learn.py --weak runs/baseline --max-cases 40 --batch-size 20
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

import slime
import run_benchmark as rb
from experience_store import ExperienceStore
from mine_experience import (
    detect_signals,
    is_failure,
    iter_traces,
    tool_sequence,
)

from openai import OpenAI


# ============================================================
# 总结模型配置（独立于跑分用的 LLM_* ）
#   LEARN_API_KEY / LEARN_API_URL / LEARN_MODEL 若未设置，则回退到
#   slime 的 LLM_API_KEY / LLM_API_URL / LLM_MODEL。
#   这样跑分和"学习总结"可以用不同的服务商 / key / 模型。
#   注：slime 在 import 时已 load_dotenv()，故此处 os.getenv 能读到 .env。
# ============================================================
LEARN_API_KEY = os.getenv("LEARN_API_KEY", "") or slime.LLM_API_KEY
LEARN_API_URL = os.getenv("LEARN_API_URL", "") or slime.LLM_API_URL
LEARN_MODEL = os.getenv("LEARN_MODEL", "") or slime.LLM_MODEL


# ============================================================
# 案例收集
# ============================================================
def collect_failure_cases(
    weak_dir: Path,
    strong_dir: Path | None,
    max_cases: int,
) -> list[dict]:
    """收集失败案例（带信号 + weak 轨迹 + 可选 strong 对照）。

    优先选取"weak 失败、strong 同题成功"的强对照案例，信息量最大。
    """
    strong = {}
    if strong_dir:
        for rec in iter_traces(strong_dir):
            strong[rec["idx"]] = rec

    cases = []
    for rec in iter_traces(weak_dir):
        if not is_failure(rec):
            continue
        sig = detect_signals(rec)
        hit_sigs = [k for k, v in sig.items()
                    if isinstance(v, bool) and v or (isinstance(v, int) and v > 0 and k not in ("rounds",))]
        srec = strong.get(rec["idx"])
        strong_ok = bool(srec) and (1 <= rb.match_rank(srec.get("answer", ""), srec["label"]) <= 1)
        cases.append({
            "idx": rec["idx"],
            "query": rec["query"],
            "label": rec["label"],
            "weak_answer": (rec.get("answer", "") or "")[:120],
            "weak_seq": tool_sequence(rec.get("trace") or {}),
            "weak_rounds": (rec.get("trace") or {}).get("rounds_used"),
            "signals": hit_sigs,
            "strong_ok": strong_ok,
            "strong_seq": tool_sequence((srec or {}).get("trace") or {}) if srec else None,
            "strong_rounds": (srec or {}).get("trace", {}).get("rounds_used") if srec else None,
        })

    # 排序：有强对照的优先，其次信号多的优先
    cases.sort(key=lambda c: (c["strong_ok"], len(c["signals"])), reverse=True)
    return cases[:max_cases]


def render_cases_for_prompt(cases: list[dict]) -> str:
    """把案例渲染成喂给 LLM 的紧凑文本。"""
    blocks = []
    for c in cases:
        lines = [
            f"### 案例 #{c['idx']}",
            f"用户提问: {c['query']}",
            f"正确答案: {c['label']}",
            f"失败轨迹({c['weak_rounds']}轮): {c['weak_seq']}",
            f"失败回答: {c['weak_answer']}",
        ]
        if c["strong_seq"]:
            tag = "✓成功" if c["strong_ok"] else "✗也失败"
            lines.append(f"对照轨迹({c['strong_rounds']}轮,{tag}): {c['strong_seq']}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


# ============================================================
# Prompt 构造
# ============================================================
SUMMARY_SYSTEM_PROMPT = """你是一名资深的 AI Agent 行为分析专家。你正在分析一个「影视问答 Agent」的失败案例。

该 Agent 的工作方式：
- 工具 web_search：搜索互联网获取影视信息（片名、演员、剧情等）
- 工具 db_search：在平台媒资库按标题(titles)检索，确认媒资是否可用（is_same_match=1 / results 非空 即命中）
- 目标：根据用户描述找到正确的影视作品，并按规则排序输出推荐卡片

轨迹符号说明：
- ws×N：本轮发起了 N 次 web_search
- db[标题1/标题2]✓：db_search 查这些标题，✓=命中库内、∅=未命中、✗err=报错（多为缺 titles 参数）
- →：表示进入下一轮

你的任务：对比失败轨迹（weak）与成功轨迹（strong），归纳出**可泛化、可执行**的经验规则，
帮助 Agent 下次避免同类失败。规则应具体到「在什么情况下应该怎么做/不该怎么做」，而非空泛建议。

输出要求：严格输出 JSON（不要任何额外文字、不要 markdown 代码块标记），格式：
{
  "rules": [
    {
      "rule": "规则文本（一句话，具体可执行，30-80字）",
      "signal": "信号标签（英文小写下划线，如 repeat_call / db_error / no_stop_after_match / plot_as_titles / many_rounds / 或你新归纳的）",
      "priority": 优先级整数1-10（该模式越普遍、影响越大越高）,
      "rationale": "为什么（简短，引用案例编号）"
    }
  ]
}
规则数量控制在 3-8 条，合并同类，去除重复，按 priority 从高到低排列。"""


def build_user_prompt(cases: list[dict], known_rules: list[str]) -> str:
    parts = [f"以下是 {len(cases)} 个失败案例（含成功对照）：\n", render_cases_for_prompt(cases)]
    if known_rules:
        parts.append("\n\n已有经验规则（请勿重复，只补充新的或更精炼的）：")
        for r in known_rules:
            parts.append(f"- {r}")
    parts.append("\n\n请分析上述案例，归纳经验规则，严格按要求输出 JSON。")
    return "\n".join(parts)


# ============================================================
# LLM 调用与解析
# ============================================================
def _parse_llm_json(text: str) -> list[dict]:
    """从 LLM 返回中提取 rules 列表，容错处理。"""
    # 去掉可能的 ```json ``` 包裹
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    # 尝试直接解析
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "rules" in data:
            return data["rules"]
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    # 兜底：抓取第一个 {...} JSON 块
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(0))
            return data.get("rules", []) if isinstance(data, dict) else []
        except json.JSONDecodeError:
            pass
    return []


def call_llm(client: OpenAI, model: str, system: str, user: str) -> str:
    """调用 LLM 做经验总结（非工具调用，纯文本生成）。"""
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=slime.LLM_MAX_TOKENS,
        temperature=0.3,  # 总结任务略带确定性
    )
    return resp.choices[0].message.content or ""


def summarize_in_batches(
    client: OpenAI,
    model: str,
    cases: list[dict],
    known_rules: list[str],
    batch_size: int,
    dry_run: bool,
) -> list[dict]:
    """分批把案例喂给 LLM 总结，合并所有批次的规则。"""
    all_rules = []
    batches = [cases[i:i + batch_size] for i in range(0, len(cases), batch_size)]
    for bi, batch in enumerate(batches, 1):
        user_prompt = build_user_prompt(batch, known_rules)
        print(f"\n--- 批次 {bi}/{len(batches)}（{len(batch)} 案例）---", file=sys.stderr)
        if dry_run:
            print("[dry-run] System prompt:\n" + SUMMARY_SYSTEM_PROMPT, file=sys.stderr)
            print("\n[dry-run] User prompt:\n" + user_prompt, file=sys.stderr)
            continue
        raw = call_llm(client, model, SUMMARY_SYSTEM_PROMPT, user_prompt)
        rules = _parse_llm_json(raw)
        if not rules:
            print(f"⚠ 批次 {bi} 未解析出规则。原始返回前 300 字：\n{raw[:300]}", file=sys.stderr)
            continue
        print(f"批次 {bi} 提取 {len(rules)} 条规则", file=sys.stderr)
        for r in rules:
            print(f"  - [{r.get('signal','')}] p={r.get('priority','?')} {r.get('rule','')[:60]}", file=sys.stderr)
        all_rules.extend(rules)
        # 把本批规则也加入 known_rules，避免下一批重复
        known_rules = known_rules + [r.get("rule", "") for r in rules]
    return all_rules


def _dedup_rules(rules: list[dict]) -> list[dict]:
    """简单去重：规则文本前 20 字相同视为重复，保留 priority 高的。"""
    seen = {}
    for r in rules:
        key = (r.get("rule", "") or "")[:20]
        if key not in seen or (r.get("priority", 0) > seen[key].get("priority", 0)):
            seen[key] = r
    return sorted(seen.values(), key=lambda r: r.get("priority", 0), reverse=True)


# ============================================================
# 主流程
# ============================================================
def main() -> None:
    ap = argparse.ArgumentParser(description="用强模型从 benchmark 轨迹智能总结经验")
    ap.add_argument("--weak", required=True, help="较弱 run 的输出目录（从中挖失败）")
    ap.add_argument("--strong", default=None, help="较好 run 的输出目录（同题成功对照）")
    ap.add_argument("--store", default="experience/rules.jsonl", help="经验库文件路径")
    ap.add_argument("--model", default=None, help="做总结的模型名（默认 LEARN_MODEL，回退 LLM_MODEL）")
    ap.add_argument("--api-key", default=None,
                    help="总结模型的 API Key（默认 LEARN_API_KEY，回退 LLM_API_KEY）")
    ap.add_argument("--api-url", default=None,
                    help="总结模型的 API 地址（默认 LEARN_API_URL，回退 LLM_API_URL；自动追加 /v1）")
    ap.add_argument("--max-cases", type=int, default=40, help="最多送入总结的失败案例数")
    ap.add_argument("--batch-size", type=int, default=20, help="每批送入 LLM 的案例数")
    ap.add_argument("--append", action="store_true", help="增量模式：把已有规则作为 known_rules 去重")
    ap.add_argument("--dry-run", action="store_true", help="只打印将发送给 LLM 的 prompt，不调用、不写入")
    args = ap.parse_args()

    weak_dir = Path(args.weak)
    strong_dir = Path(args.strong) if args.strong else None
    # 总结模型的凭证：CLI 参数 > LEARN_* 环境变量 > LLM_* 回退
    model = args.model or LEARN_MODEL
    api_key = args.api_key or LEARN_API_KEY
    api_url = args.api_url or LEARN_API_URL

    # 收集失败案例
    cases = collect_failure_cases(weak_dir, strong_dir, args.max_cases)
    if not cases:
        print("未发现失败案例（或 traces.jsonl 为空）。", file=sys.stderr)
        return
    n_strong = sum(1 for c in cases if c["strong_ok"])
    print(f"收集到 {len(cases)} 个失败案例（{n_strong} 个有成功对照），"
          f"总结模型={model} @ {slime._mask(api_key)} {api_url}", file=sys.stderr)

    # 加载已有经验库
    store = ExperienceStore(args.store)
    store.load()
    known_rules = [e.rule for e in store.entries] if args.append else []

    # 构造 client（dry-run 时不需要真实 key）
    client = None
    if not args.dry_run:
        if not api_key:
            print("❌ 未配置总结模型的 API Key（LEARN_API_KEY / LLM_API_KEY 均为空，"
                  "也未传 --api-key）。可加 --dry-run 仅预览 prompt。", file=sys.stderr)
            sys.exit(1)
        client = OpenAI(api_key=api_key, base_url=f"{api_url}/v1")

    # 分批总结
    raw_rules = summarize_in_batches(
        client, model, cases, known_rules, args.batch_size, args.dry_run
    )

    if args.dry_run:
        print("\n[dry-run] 结束，未调用模型、未写入。", file=sys.stderr)
        return

    if not raw_rules:
        print("模型未产出可解析的规则。", file=sys.stderr)
        return

    # 去重 + 写入
    final_rules = _dedup_rules(raw_rules)
    source = f"llm-summarized by {model} from weak={weak_dir}" + (f" vs strong={strong_dir}" if strong_dir else "")
    print(f"\n合并去重后 {len(final_rules)} 条规则，写入经验库：", file=sys.stderr)
    for r in final_rules:
        store.add(
            rule=r.get("rule", "").strip(),
            signal=r.get("signal", "").strip(),
            source=source,
            priority=int(r.get("priority", 5)),
            examples=[],
        )
        print(f"  + [{r.get('signal','')}] p={r.get('priority','?')} {r.get('rule','')[:60]}", file=sys.stderr)
        if r.get("rationale"):
            print(f"      理由: {r['rationale'][:80]}", file=sys.stderr)

    store.save()
    print(f"\n✅ 已写入经验库: {store.path}（共 {len(store)} 条）", file=sys.stderr)


if __name__ == "__main__":
    main()
