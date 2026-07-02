"""
Experience Learning 自动迭代脚本。

一键完成完整闭环：
  1. baseline 跑分（无经验注入）
  2. 从 baseline 失败中学习，生成经验库
  3. 注入经验后再跑分
  4. 对比两次结果

用法：
  # 完整闭环（使用现有 strong run 做对比）
  python run_experience_loop.py --strong runs/exp2 --csv benchmark_sample100.csv -c 8

  # 只学习+跑分（已有 baseline）
  python run_experience_loop.py --baseline runs/exp1 --strong runs/exp2 -c 8

  # 多轮迭代
  python run_experience_loop.py --iterations 3 --strong runs/exp2 -c 4

环境变量 SLIME_EXPERIENCE_FILE 会被本脚本管理，请勿同时手动设置。
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def _run_cmd(cmd: list[str], env: dict | None = None) -> int:
    """执行子进程命令，继承 stderr/stdout。"""
    full_env = {**os.environ, **(env or {})}
    print(f"\n{'='*60}", file=sys.stderr)
    print(f"执行: {' '.join(cmd)}", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    result = subprocess.run(cmd, env=full_env)
    return result.returncode


def _load_summary(run_dir: Path) -> dict:
    """读取 run 目录的 summary.json。"""
    p = run_dir / "summary.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


def _read_model_identity(run_dir: Path) -> tuple[str, str]:
    """从 run 的 config.json 读取 (model, api_url)，用于校验模型身份是否一致。"""
    cfg = run_dir / "config.json"
    if cfg.exists():
        try:
            c = json.loads(cfg.read_text(encoding="utf-8"))
            llm = c.get("llm", {})
            return str(llm.get("model", "")), str(llm.get("api_url", ""))
        except (json.JSONDecodeError, OSError):
            pass
    return "", ""


def _verify_identity(run_dir: Path, model: str, url: str, phase: str) -> None:
    """核对某次跑分实际用的模型/endpoint 是否与锁定值一致，不一致则中止。

    防止 baseline 与 iter 跑在不同模型上——否则"提升"可能只是换了模型，
    而非经验注入的效果（详见 4b 目录那次被污染的对照）。
    """
    m, u = _read_model_identity(run_dir)
    if (m, u) != (model, url):
        print(f"\n❌ 模型身份不一致，对比无效，已中止！", file=sys.stderr)
        print(f"   锁定（baseline）: {model} @ {url}", file=sys.stderr)
        print(f"   实际（{phase}）  : {m} @ {u}", file=sys.stderr)
        print(f"   请确保整轮闭环 baseline 与 iter 跑在同一模型/endpoint 上，"
              f"中途不要改 LLM_MODEL / LLM_API_URL 或切换后端。", file=sys.stderr)
        sys.exit(2)


def _compare(baseline_dir: Path, improved_dir: Path) -> str:
    """对比两次 run 的指标差异。"""
    b = _load_summary(baseline_dir)
    i = _load_summary(improved_dir)
    if not b or not i:
        return "（无法对比，缺少 summary.json）"

    # (key, 显示名, 类型)；类型：rate=百分比 / num=两位小数 / int=整数
    metrics = [
        ("ok_rate", "ok_rate", "rate"),
        ("hit@1_rate", "hit@1", "rate"),
        ("hit@3_rate", "hit@3", "rate"),
        ("hit@5_rate", "hit@5", "rate"),
        ("hit@all_rate", "hit@all", "rate"),
        ("avg_rounds", "avg_rounds", "num"),
        ("avg_elapsed", "avg_elapsed(s)", "num"),
        ("avg_llm_s", "avg_llm(s)", "num"),
        ("avg_tool_s", "avg_tool(s)", "num"),
        ("avg_total_tokens", "avg_tokens", "int"),
        ("wall_seconds", "wall(s)", "num"),
    ]
    lines = ["\n📊 对比结果：",
             f"{'指标':<16} {'baseline':>10} {'+ experience':>14} {'Δ':>10}"]
    lines.append("-" * 54)
    for key, name, kind in metrics:
        bv = b.get(key, 0) or 0
        iv = i.get(key, 0) or 0
        delta = iv - bv
        sign = "+" if delta > 0 else ""
        if kind == "rate":
            lines.append(f"{name:<16} {bv:>9.1%} {iv:>13.1%} {sign}{delta:>8.1%}")
        elif kind == "int":
            lines.append(f"{name:<16} {bv:>10.0f} {iv:>14.0f} {sign}{delta:>9.0f}")
        else:
            lines.append(f"{name:<16} {bv:>10.2f} {iv:>14.2f} {sign}{delta:>9.2f}")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="Experience Learning 自动迭代闭环")
    ap.add_argument("--csv", default="benchmark_sample100.csv", help="benchmark CSV")
    ap.add_argument("-c", "--concurrency", type=int, default=4, help="跑分并发数")
    ap.add_argument("--strong", default=None, help="作为正例参照的 run 目录（用于对比学习）")
    ap.add_argument("--baseline", default=None, help="已有的 baseline run 目录（跳过第 1 步）")
    ap.add_argument("--iterations", type=int, default=1, help="迭代轮数（每轮学习+跑分）")
    ap.add_argument("--out", default="experience_loop", help="输出根目录")
    ap.add_argument("--seed", type=int, default=42, help="benchmark shuffle seed")
    ap.add_argument("--shuffle", action="store_true", help="是否 shuffle benchmark")
    ap.add_argument("-n", "--limit", type=int, default=None, help="只跑 N 条")
    ap.add_argument("--llm", action="store_true",
                    help="用强模型总结经验（llm_learn.py）而非规则模板（learn_from_run.py）")
    ap.add_argument("--llm-model", default=None, help="--llm 模式下做总结的模型名")
    args = ap.parse_args()

    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # ---- 锁定模型身份：整轮闭环的所有 benchmark 必须跑在同一模型/endpoint 上 ----
    # 否则 baseline 与 iter 跑在不同模型上，"提升"可能只是换了模型而非经验之功。
    # import slime 会触发 load_dotenv()，得到 .env 生效后的模型配置。
    import slime  # noqa: E402
    if args.baseline:
        # 复用已有 baseline：以它 config.json 记录的模型为准，后续 iter 必须与之一致。
        pinned_model, pinned_url = _read_model_identity(Path(args.baseline))
        if not pinned_model:
            pinned_model, pinned_url = slime.LLM_MODEL, slime.LLM_API_URL
        if (slime.LLM_MODEL, slime.LLM_API_URL) != (pinned_model, pinned_url):
            print(f"⚠ 当前 .env 模型（{slime.LLM_MODEL} @ {slime.LLM_API_URL}）"
                  f"与 baseline（{pinned_model} @ {pinned_url}）不一致；"
                  f"将强制后续跑分沿用 baseline 的模型以保证可比。", file=sys.stderr)
    else:
        pinned_model, pinned_url = slime.LLM_MODEL, slime.LLM_API_URL

    # 强制注入每个 benchmark 子进程；load_dotenv(override=False) 不会覆盖已存在的
    # 环境变量，因此即使中途改了 .env，也以此处锁定的值为准。
    model_env = {"LLM_MODEL": pinned_model, "LLM_API_URL": pinned_url}
    print(f"🔒 已锁定模型：{pinned_model} @ {pinned_url}"
          f"（本轮所有 benchmark 强制使用，中途改 .env / 切后端不生效）", file=sys.stderr)

    # 构建通用 benchmark 参数
    bench_args = [args.csv, "-c", str(args.concurrency)]
    if args.shuffle:
        bench_args += ["--shuffle", "--seed", str(args.seed)]
    if args.limit:
        bench_args += ["-n", str(args.limit)]

    # Step 1: baseline（无经验注入）
    if args.baseline:
        baseline_dir = Path(args.baseline)
        print(f"使用已有 baseline: {baseline_dir}", file=sys.stderr)
    else:
        baseline_dir = out_root / f"baseline_{timestamp}"
        cmd = [sys.executable, "run_benchmark.py"] + bench_args + ["-d", str(baseline_dir)]
        # 确保不注入经验 + 锁定模型
        env = {"SLIME_EXPERIENCE_FILE": "", **model_env}
        ret = _run_cmd(cmd, env=env)
        if ret != 0:
            print(f"❌ baseline 跑分失败 (exit={ret})", file=sys.stderr)
            sys.exit(1)
        _verify_identity(baseline_dir, pinned_model, pinned_url, "baseline")
        print(f"✅ baseline 完成: {baseline_dir}", file=sys.stderr)

    # 迭代学习
    current_weak = baseline_dir
    experience_file = out_root / "rules.jsonl"

    for iteration in range(1, args.iterations + 1):
        print(f"\n{'#'*60}", file=sys.stderr)
        print(f"# 迭代 {iteration}/{args.iterations}", file=sys.stderr)
        print(f"{'#'*60}", file=sys.stderr)

        # Step 2: 从 weak run 学习经验
        if args.llm:
            learn_cmd = [
                sys.executable, "llm_learn.py",
                "--weak", str(current_weak),
                "--store", str(experience_file),
            ]
            if args.llm_model:
                learn_cmd += ["--model", args.llm_model]
        else:
            learn_cmd = [
                sys.executable, "learn_from_run.py",
                "--weak", str(current_weak),
                "--store", str(experience_file),
            ]
        if args.strong:
            learn_cmd += ["--strong", args.strong]
        if iteration > 1:
            learn_cmd.append("--append")  # 增量学习

        ret = _run_cmd(learn_cmd)
        if ret != 0:
            print(f"⚠ 学习步骤退出 (exit={ret})，可能无新规则", file=sys.stderr)

        # 检查经验库是否非空
        if not experience_file.exists() or experience_file.stat().st_size == 0:
            print("经验库为空，跳过后续迭代。", file=sys.stderr)
            break

        # Step 3: 注入经验后跑分（锁定同一模型）
        improved_dir = out_root / f"iter{iteration}_{timestamp}"
        cmd = [sys.executable, "run_benchmark.py"] + bench_args + [
            "-d", str(improved_dir),
            "--experience", str(experience_file),
        ]
        ret = _run_cmd(cmd, env=model_env)
        if ret != 0:
            print(f"❌ 迭代 {iteration} 跑分失败 (exit={ret})", file=sys.stderr)
            sys.exit(1)
        _verify_identity(improved_dir, pinned_model, pinned_url, f"iter{iteration}")
        print(f"✅ 迭代 {iteration} 完成: {improved_dir}", file=sys.stderr)

        # Step 4: 对比
        comparison = _compare(baseline_dir, improved_dir)
        print(comparison, file=sys.stderr)

        # 保存对比结果
        (out_root / f"comparison_iter{iteration}.txt").write_text(
            comparison, encoding="utf-8"
        )

        # 为下一轮迭代准备
        current_weak = improved_dir

    # 最终总结
    print(f"\n{'='*60}", file=sys.stderr)
    print("✅ Experience Learning 闭环完成", file=sys.stderr)
    print(f"  经验库: {experience_file}", file=sys.stderr)
    print(f"  产物目录: {out_root}", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)


if __name__ == "__main__":
    main()
