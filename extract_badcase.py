"""从某个 run 的 result.csv 提取 hit@1 失败案例，整理成 badcase.csv。

用法：
  python extract_badcase.py <run_dir> [-o badcase.csv]
"""
import argparse
import csv
import sys
from pathlib import Path

# 输出保留的列：query,label 必须在最前两列，以兼容 run_benchmark.py 的
# load_cases（它固定读 row[0]=query、row[1]=label，其余列忽略）。
# 这样同一个文件既能喂 benchmark，又保留 idx/rank 等给人分析。
COLS = ["query", "label", "idx", "rank", "hit3", "hit5", "hit_all",
        "rounds", "total_tokens", "elapsed_s", "answer"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir", help="benchmark run 目录（含 result.csv）")
    ap.add_argument("-o", "--out", default="badcase.csv", help="输出 CSV")
    args = ap.parse_args()

    src = Path(args.run_dir) / "result.csv"
    if not src.exists():
        print(f"❌ 找不到 {src}", file=sys.stderr)
        sys.exit(1)

    rows = []
    with open(src, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            if str(r.get("hit1", "")).strip() == "0":   # hit@1 未命中
                rows.append({c: (r.get(c, "") or "").strip() for c in COLS})

    # rank 升序（rank=-1/大的排后），便于先看“差一点”的
    def _rank_key(r):
        try:
            v = int(r["rank"])
        except (ValueError, TypeError):
            return 10**9
        return v if v > 0 else 10**8   # 未命中(0/-1)排最后
    rows.sort(key=_rank_key)

    with open(args.out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        w.writerows(rows)

    # 简单统计
    total = 0
    with open(src, encoding="utf-8-sig", newline="") as f:
        total = sum(1 for _ in csv.DictReader(f))
    recovered = sum(1 for r in rows if r["hit_all"] == "1")   # hit@1 挂但命中卡片其他名次
    total_miss = sum(1 for r in rows if r["hit_all"] != "1")  # 完全没命中卡片
    print(f"来源: {src}", file=sys.stderr)
    print(f"总用例 {total}，hit@1 失败 {len(rows)} 条 → {args.out}", file=sys.stderr)
    print(f"  其中 hit@all 命中(仅名次靠后) {recovered} 条；完全未命中卡片 {total_miss} 条",
          file=sys.stderr)


if __name__ == "__main__":
    main()
