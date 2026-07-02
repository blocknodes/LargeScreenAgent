"""
从 benchmark 轨迹自动学习经验——experience learning 闭环。

升级版 mine_experience.py：不再只输出 markdown 审阅稿，而是：
  1. 检测失败模式（复用 mine_experience 的信号检测逻辑）
  2. 自动生成结构化经验条目，写入 experience/rules.jsonl
  3. 支持增量学习（新 run 的经验追加到已有经验库）
  4. 支持 --dry-run 预览不写入

完整闭环：
  跑分 → 学习 → 注入 prompt → 再跑分 → 对比 → 再学习 ...

用法：
  # 第一次学习：从 weak vs strong 对比中提取经验
  python learn_from_run.py --weak runs/exp1 --strong runs/exp2

  # 增量学习：新 run 的失败追加新经验（不重复已有规则）
  python learn_from_run.py --weak runs/exp3 --strong runs/exp2 --append

  # 预览模式（不写入经验库）
  python learn_from_run.py --weak runs/exp1 --strong runs/exp2 --dry-run

  # 指定经验库路径
  python learn_from_run.py --weak runs/exp1 --strong runs/exp2 --store experience/v2.jsonl

  # 同时输出旧版 insights.md（兼容）
  python learn_from_run.py --weak runs/exp1 --strong runs/exp2 --insights
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import run_benchmark as rb
from experience_store import ExperienceStore
from mine_experience import (
    RULE_TEMPLATES,
    SIGNAL_TO_RULE,
    detect_signals,
    is_failure,
    iter_traces,
    tool_sequence,
)


def _dedup_rules(store: ExperienceStore, new_rules: list[dict]) -> list[dict]:
    """去重：如果经验库已有相同 signal 的规则，跳过（避免重复注入）。"""
    existing_signals = {e.signal for e in store.entries if e.signal}
    return [r for r in new_rules if r["signal"] not in existing_signals]


def learn(
    weak_dir: Path,
    strong_dir: Path | None,
    store: ExperienceStore,
    max_examples: int = 3,
    append: bool = False,
    min_ratio: float = 0.05,
) -> list[dict]:
    """从 weak/strong 对比中提取经验条目。

    Args:
        weak_dir: 较弱 run 的输出目录
        strong_dir: 较好 run 的输出目录（可选）
        store: 经验库实例（已 load）
        max_examples: 每条规则最多保留的对照实例数
        append: True 则跳过已有 signal 的规则
        min_ratio: 失败模式占比低于此值时不生成规则（去噪）

    Returns:
        新生成的经验条目列表（dict 格式）
    """
    # 读 strong run 建索引
    strong = {}
    if strong_dir:
        for rec in iter_traces(strong_dir):
            strong[rec["idx"]] = rec

    # 扫描 weak run
    total = 0
    failures = []
    sig_count = Counter()
    rule_examples = defaultdict(list)

    for rec in iter_traces(weak_dir):
        total += 1
        if not is_failure(rec):
            continue
        sig = detect_signals(rec)
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
        }
        failures.append(item)
        for s in hit_sigs:
            sig_count[s] += 1
            rule = SIGNAL_TO_RULE[s]
            if strong_ok and len(rule_examples[rule]) < max_examples:
                rule_examples[rule].append(item)

    # 兜底补实例
    for s in sig_count:
        rule = SIGNAL_TO_RULE[s]
        if len(rule_examples[rule]) < max_examples:
            for it in failures:
                if it["signals"].get(s) and it not in rule_examples[rule]:
                    rule_examples[rule].append(it)
                    if len(rule_examples[rule]) >= max_examples:
                        break

    n_fail = len(failures)
    source = f"mined from weak={weak_dir}" + (f" vs strong={strong_dir}" if strong_dir else "")

    # 生成经验条目
    new_rules = []
    rule_score = Counter()
    for s, c in sig_count.items():
        rule_score[SIGNAL_TO_RULE[s]] += c

    for rule_key, count in rule_score.most_common():
        # 占比过低的信号不生成规则
        ratio = count / max(n_fail, 1)
        if ratio < min_ratio:
            continue
        # 确定优先级：占比越高越重要
        if ratio >= 0.3:
            priority = 9
        elif ratio >= 0.15:
            priority = 7
        else:
            priority = 5

        examples = []
        for it in rule_examples.get(rule_key, []):
            examples.append({
                "query": it["query"],
                "label": it["label"],
                "weak_seq": it["weak_seq"],
                "strong_seq": it.get("strong_seq", ""),
            })

        # 找到对应的 signal 标签（多个 signal 可能映射同一 rule_key）
        signal = rule_key  # 用 rule_key 本身作为 signal 标识

        new_rules.append({
            "rule": RULE_TEMPLATES[rule_key],
            "signal": signal,
            "source": source,
            "examples": examples,
            "priority": priority,
        })

    # 去重
    if append:
        new_rules = _dedup_rules(store, new_rules)

    return new_rules


def main() -> None:
    ap = argparse.ArgumentParser(description="从 benchmark 轨迹自动学习经验（experience learning）")
    ap.add_argument("--weak", required=True, help="较弱 run 的输出目录")
    ap.add_argument("--strong", default=None, help="较好 run 的输出目录（同题成功对照）")
    ap.add_argument("--store", default="experience/rules.jsonl", help="经验库文件路径")
    ap.add_argument("--append", action="store_true", help="增量模式：跳过已有 signal 的规则")
    ap.add_argument("--dry-run", action="store_true", help="预览模式，不写入经验库")
    ap.add_argument("--max-examples", type=int, default=3, help="每条规则最多保留的对照实例数")
    ap.add_argument("--min-ratio", type=float, default=0.05, help="失败模式占比阈值（低于此不生成规则）")
    ap.add_argument("--insights", action="store_true", help="同时输出旧版 insights.md（兼容）")
    ap.add_argument("--out", default=None, help="insights.md 输出目录（默认同 --store 目录）")
    args = ap.parse_args()

    weak_dir = Path(args.weak)
    strong_dir = Path(args.strong) if args.strong else None

    # 加载已有经验库
    store = ExperienceStore(args.store)
    store.load()
    print(f"已有经验库: {store.render_summary()}", file=sys.stderr)

    # 学习新经验
    new_rules = learn(
        weak_dir=weak_dir,
        strong_dir=strong_dir,
        store=store,
        max_examples=args.max_examples,
        append=args.append,
        min_ratio=args.min_ratio,
    )

    if not new_rules:
        print("未发现新的失败模式（或全部已在经验库中），无新规则可添加。", file=sys.stderr)
        return

    # 展示新规则
    print(f"\n新提取 {len(new_rules)} 条经验规则：", file=sys.stderr)
    for i, r in enumerate(new_rules, 1):
        print(f"  {i}. [{r['signal']}] p={r['priority']} {r['rule'][:60]}...", file=sys.stderr)

    # 写入经验库
    if not args.dry_run:
        for r in new_rules:
            store.add(
                rule=r["rule"],
                signal=r["signal"],
                source=r["source"],
                examples=r["examples"],
                priority=r["priority"],
            )
        store.save()
        print(f"\n✅ 已写入经验库: {store.path} (共 {len(store)} 条)", file=sys.stderr)
    else:
        print("\n[dry-run] 未写入经验库。", file=sys.stderr)

    # 预览注入 prompt 效果
    print("\n--- 注入 prompt 预览 ---", file=sys.stderr)
    # 临时把新规则加入并渲染
    temp_store = ExperienceStore(args.store)
    temp_store.load()
    if args.dry_run:
        for r in new_rules:
            temp_store.add(**r)
    block = temp_store.render_prompt_block(include_examples=True) if len(temp_store) else store.render_prompt_block(include_examples=True)
    print(block or "(空)", file=sys.stderr)

    # 兼容：输出 insights.md
    if args.insights:
        out_dir = Path(args.out) if args.out else store.path.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        # 调用原有 mine_experience 逻辑
        import mine_experience
        sys.argv = ["mine_experience", "--weak", str(weak_dir)]
        if strong_dir:
            sys.argv += ["--strong", str(strong_dir)]
        sys.argv += ["--out", str(out_dir)]
        mine_experience.main()


if __name__ == "__main__":
    main()
