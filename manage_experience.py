"""
经验库管理 CLI 工具。

用法：
  python manage_experience.py list                     # 列出所有经验条目
  python manage_experience.py show                     # 渲染 prompt 注入预览
  python manage_experience.py add "规则文本" --signal repeat_call --priority 8
  python manage_experience.py disable <id>             # 禁用某条
  python manage_experience.py enable <id>              # 启用某条
  python manage_experience.py remove <id>              # 删除某条
  python manage_experience.py stats                    # 统计信息
  python manage_experience.py export --format prompt   # 导出为可粘贴的 prompt 片段
  python manage_experience.py import insights.md       # （未来）从 insights.md 导入
"""

import argparse
import json
import sys
from pathlib import Path

from experience_store import ExperienceStore


def cmd_list(store: ExperienceStore, args) -> None:
    if not store.entries:
        print("经验库为空。")
        return
    for e in store.entries:
        state = "✓" if e.enabled else "✗"
        print(f"  [{state}] id={e.id}  p={e.priority}  signal={e.signal}")
        print(f"      {e.rule[:80]}{'...' if len(e.rule) > 80 else ''}")
        if args.verbose and e.examples:
            for ex in e.examples[:2]:
                print(f"      例: {ex.get('query', '')[:40]}")
        print()


def cmd_show(store: ExperienceStore, args) -> None:
    block = store.render_prompt_block(include_examples=args.examples)
    if block:
        print(block)
    else:
        print("（无启用的经验规则）")


def cmd_add(store: ExperienceStore, args) -> None:
    entry = store.add(
        rule=args.rule,
        signal=args.signal or "",
        source=args.source or "manual",
        priority=args.priority,
    )
    store.save()
    print(f"已添加: id={entry.id}")


def cmd_disable(store: ExperienceStore, args) -> None:
    for e in store.entries:
        if e.id == args.id:
            e.enabled = False
            store.save()
            print(f"已禁用: {e.id}")
            return
    print(f"未找到 id={args.id}")


def cmd_enable(store: ExperienceStore, args) -> None:
    for e in store.entries:
        if e.id == args.id:
            e.enabled = True
            store.save()
            print(f"已启用: {e.id}")
            return
    print(f"未找到 id={args.id}")


def cmd_remove(store: ExperienceStore, args) -> None:
    if store.remove(args.id):
        store.save()
        print(f"已删除: {args.id}")
    else:
        print(f"未找到 id={args.id}")


def cmd_stats(store: ExperienceStore, args) -> None:
    print(store.render_summary())
    print()
    enabled = store.get_enabled()
    print(f"启用条目数: {len(enabled)}")
    print(f"总条目数: {len(store)}")
    if enabled:
        print(f"优先级范围: {min(e.priority for e in enabled)} - {max(e.priority for e in enabled)}")
        # prompt 字数估算
        block = store.render_prompt_block()
        print(f"注入 prompt 约 {len(block)} 字符")


def cmd_export(store: ExperienceStore, args) -> None:
    if args.format == "prompt":
        block = store.render_prompt_block(include_examples=args.examples)
        print(block or "（空）")
    elif args.format == "json":
        data = [e.to_dict() for e in store.get_enabled()]
        print(json.dumps(data, ensure_ascii=False, indent=2))
    elif args.format == "rules":
        for e in store.get_enabled():
            print(f"- {e.rule}")
    else:
        print(f"未知格式: {args.format}")


def main() -> None:
    parser = argparse.ArgumentParser(description="经验库管理工具")
    parser.add_argument("--store", default="experience/rules.jsonl", help="经验库文件路径")

    sub = parser.add_subparsers(dest="command")

    # list
    p = sub.add_parser("list", help="列出所有经验条目")
    p.add_argument("-v", "--verbose", action="store_true", help="显示实例")

    # show
    p = sub.add_parser("show", help="渲染 prompt 注入预览")
    p.add_argument("--examples", action="store_true", help="包含对照实例")

    # add
    p = sub.add_parser("add", help="手动添加经验")
    p.add_argument("rule", help="规则文本")
    p.add_argument("--signal", default="", help="行为信号标签")
    p.add_argument("--source", default="manual", help="来源描述")
    p.add_argument("--priority", type=int, default=5, help="优先级 1-10")

    # disable / enable / remove
    for name in ("disable", "enable", "remove"):
        p = sub.add_parser(name, help=f"{name} 经验条目")
        p.add_argument("id", help="经验条目 ID")

    # stats
    sub.add_parser("stats", help="统计信息")

    # export
    p = sub.add_parser("export", help="导出经验")
    p.add_argument("--format", choices=["prompt", "json", "rules"], default="prompt")
    p.add_argument("--examples", action="store_true", help="包含对照实例")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    store = ExperienceStore(args.store).load()
    handler = {
        "list": cmd_list,
        "show": cmd_show,
        "add": cmd_add,
        "disable": cmd_disable,
        "enable": cmd_enable,
        "remove": cmd_remove,
        "stats": cmd_stats,
        "export": cmd_export,
    }
    handler[args.command](store, args)


if __name__ == "__main__":
    main()
