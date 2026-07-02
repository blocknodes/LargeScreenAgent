"""
经验库管理模块（experience store）。

经验条目以 JSONL 格式存储在文件中，每条经验包含：
  - id:        唯一标识（自动生成，时间戳+序号）
  - rule:      精炼的规则文本（可直接注入 system prompt）
  - signal:    触发该规则的行为信号标签（repeat_call, db_error, no_stop_after_match 等）
  - source:    经验来源描述（如 "mined from runs/exp1 vs runs/exp2"）
  - examples:  对照实例列表 [{query, weak_seq, strong_seq}]（可选，供审阅/few-shot）
  - enabled:   是否启用（默认 true，方便 A/B 开关单条经验）
  - priority:  优先级（1-10，越高越靠前注入 prompt，默认 5）
  - created_at: 创建时间

用法：
    from experience_store import ExperienceStore
    store = ExperienceStore("experience/rules.jsonl")
    store.load()
    store.add(rule="...", signal="repeat_call", source="...", examples=[...])
    store.save()
    prompt_block = store.render_prompt_block()  # 生成可注入 system prompt 的文本
"""

import json
import os
from datetime import datetime
from pathlib import Path


class ExperienceEntry:
    """单条经验。"""

    def __init__(
        self,
        rule: str,
        signal: str = "",
        source: str = "",
        examples: list[dict] | None = None,
        enabled: bool = True,
        priority: int = 5,
        entry_id: str = "",
        created_at: str = "",
    ):
        self.id = entry_id or datetime.now().strftime("%Y%m%d_%H%M%S_") + f"{id(self) % 10000:04d}"
        self.rule = rule
        self.signal = signal
        self.source = source
        self.examples = examples or []
        self.enabled = enabled
        self.priority = priority
        self.created_at = created_at or datetime.now().isoformat(timespec="seconds")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "rule": self.rule,
            "signal": self.signal,
            "source": self.source,
            "examples": self.examples,
            "enabled": self.enabled,
            "priority": self.priority,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ExperienceEntry":
        return cls(
            entry_id=d.get("id", ""),
            rule=d.get("rule", ""),
            signal=d.get("signal", ""),
            source=d.get("source", ""),
            examples=d.get("examples", []),
            enabled=d.get("enabled", True),
            priority=d.get("priority", 5),
            created_at=d.get("created_at", ""),
        )

    def __repr__(self) -> str:
        state = "ON" if self.enabled else "OFF"
        return f"<Experience [{state}] p={self.priority} signal={self.signal} rule={self.rule[:40]}...>"


class ExperienceStore:
    """经验库：从 JSONL 文件加载/保存/管理经验条目。"""

    def __init__(self, path: str | Path = "experience/rules.jsonl"):
        self.path = Path(path)
        self.entries: list[ExperienceEntry] = []

    def load(self) -> "ExperienceStore":
        """从文件加载经验条目。文件不存在则为空库。"""
        self.entries = []
        if not self.path.exists():
            return self
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    self.entries.append(ExperienceEntry.from_dict(json.loads(line)))
        return self

    def save(self) -> None:
        """保存全部条目到文件。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            for entry in self.entries:
                f.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")

    def add(self, rule: str, signal: str = "", source: str = "",
            examples: list[dict] | None = None, enabled: bool = True,
            priority: int = 5) -> ExperienceEntry:
        """添加一条经验。"""
        entry = ExperienceEntry(
            rule=rule, signal=signal, source=source,
            examples=examples, enabled=enabled, priority=priority,
        )
        self.entries.append(entry)
        return entry

    def remove(self, entry_id: str) -> bool:
        """按 id 删除经验。"""
        before = len(self.entries)
        self.entries = [e for e in self.entries if e.id != entry_id]
        return len(self.entries) < before

    def get_enabled(self) -> list[ExperienceEntry]:
        """获取所有启用的经验，按优先级降序排列。"""
        return sorted(
            [e for e in self.entries if e.enabled],
            key=lambda e: e.priority,
            reverse=True,
        )

    def render_prompt_block(self, max_entries: int = 20, include_examples: bool = False) -> str:
        """渲染可注入 system prompt 的经验规则文本块。

        Args:
            max_entries: 最多注入的规则数（避免 prompt 过长）
            include_examples: 是否附带对照实例（few-shot 风格）

        Returns:
            格式化的文本块，如果没有启用的经验则返回空字符串。
        """
        enabled = self.get_enabled()[:max_entries]
        if not enabled:
            return ""

        lines = ["## 经验规则（从历史失败中学习，必须遵守）", ""]
        for i, entry in enumerate(enabled, 1):
            lines.append(f"{i}. {entry.rule}")
            if include_examples and entry.examples:
                for ex in entry.examples[:2]:  # 最多展示 2 个实例
                    lines.append(f"   - 例：「{ex.get('query', '')[:30]}」")
                    if ex.get("weak_seq"):
                        lines.append(f"     ✗ {ex['weak_seq']}")
                    if ex.get("strong_seq"):
                        lines.append(f"     ✓ {ex['strong_seq']}")
        lines.append("")
        return "\n".join(lines)

    def render_summary(self) -> str:
        """生成库概览摘要。"""
        total = len(self.entries)
        enabled = len([e for e in self.entries if e.enabled])
        signals = {}
        for e in self.entries:
            if e.signal:
                signals[e.signal] = signals.get(e.signal, 0) + 1
        parts = [f"经验库: {self.path} ({enabled}/{total} 启用)"]
        if signals:
            parts.append("信号分布: " + ", ".join(f"{k}={v}" for k, v in sorted(signals.items(), key=lambda x: -x[1])))
        return "\n".join(parts)

    def __len__(self) -> int:
        return len(self.entries)

    def __repr__(self) -> str:
        return f"<ExperienceStore path={self.path} entries={len(self.entries)}>"
