"""
从 benchmark CSV 里随机抽样 N 条，保存为新的 CSV（用于快速跑分）。

用法：
    python sample_benchmark.py                                  # 默认抽 100 条 → benchmark_sample100.csv
    python sample_benchmark.py -n 50 --seed 7 -o quick.csv      # 抽 50 条、固定种子、指定输出
    python sample_benchmark.py benchmark_0601.csv -n 100        # 指定输入

输出为干净的两列（query,label），run_benchmark.py 可直接读取。
"""

import argparse
import csv
import random
from pathlib import Path

from run_benchmark import load_cases  # 复用健壮的解析（兼容 BOM/表头/空尾列）


def main() -> None:
    ap = argparse.ArgumentParser(description="随机抽样 benchmark 子集")
    ap.add_argument("csv", nargs="?", default="benchmark_0601.csv", help="输入 CSV")
    ap.add_argument("-n", "--num", type=int, default=100, help="抽样条数（默认 100）")
    ap.add_argument("--seed", type=int, default=42, help="随机种子（默认 42，结果可复现）")
    ap.add_argument("-o", "--output", default=None, help="输出 CSV（默认 benchmark_sample<N>.csv）")
    args = ap.parse_args()

    src = Path(args.csv)
    if not src.exists():
        raise SystemExit(f"找不到输入文件：{src}")

    cases = load_cases(src)
    if not cases:
        raise SystemExit("没有读到任何用例。")

    rng = random.Random(args.seed)
    rng.shuffle(cases)
    sample = cases[: args.num]

    out = Path(args.output) if args.output else Path(f"benchmark_sample{len(sample)}.csv")
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["query", "label"])
        for c in sample:
            w.writerow([c["query"], c["label"]])

    print(f"从 {len(cases)} 条中随机抽样 {len(sample)} 条（seed={args.seed}）→ {out}")


if __name__ == "__main__":
    main()
