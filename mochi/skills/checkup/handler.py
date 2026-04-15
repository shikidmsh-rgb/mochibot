"""Checkup skill — thin wrapper around mochi.checkup_core."""

import logging
from mochi.skills.base import Skill, SkillContext, SkillResult

log = logging.getLogger(__name__)


def _fmt_size(b: int) -> str:
    """Format bytes as human-readable string."""
    if b >= 1024 * 1024:
        return f"{b / 1024 / 1024:.1f} MB"
    return f"{b / 1024:.1f} KB"


def _format_markdown(data: dict) -> str:
    """Convert checkup dict to a readable markdown report."""
    lines = ["## 系统体检报告\n"]

    # ── Prompt size ──
    ps = data.get("prompt_size")
    if isinstance(ps, dict) and "error" not in ps:
        lines.append("### Prompt 体积")
        lines.append("| 项目 | 字符 | Token |")
        lines.append("|------|------|-------|")
        for name, info in ps.get("identity_prompts", {}).items():
            short = name.split("/")[-1] if "/" in name else name
            lines.append(f"| {short} | {info['chars']} | {info['tokens']} |")
        lines.append(f"| **合计** | | **{ps.get('identity_total_tokens', '?')}** |")
        cm = ps.get("core_memory", {})
        budget_mark = " ⚠ 超出预算" if cm.get("over_budget") else ""
        lines.append(f"\nCore Memory: {cm.get('tokens', '?')} / {cm.get('max_tokens', '?')} tokens{budget_mark}\n")
    elif isinstance(ps, dict):
        lines.append(f"### Prompt 体积\n❌ {ps.get('error', 'unknown error')}\n")

    # ── Database ──
    db = data.get("database")
    if isinstance(db, dict) and "error" not in db:
        lines.append("### 数据库")
        integrity = "✓ ok" if db.get("integrity_ok") else "✗ 异常"
        lines.append(f"文件大小: {_fmt_size(db.get('file_size_bytes', 0))} | 完整性: {integrity}")
        lines.append("| 表 | 行数 |")
        lines.append("|-----|------|")
        for table, count in db.get("table_counts", {}).items():
            lines.append(f"| {table} | {count} |")
        lines.append("")
    elif isinstance(db, dict):
        lines.append(f"### 数据库\n❌ {db.get('error', 'unknown error')}\n")

    # ── Memory ──
    mem = data.get("memory")
    if isinstance(mem, dict) and "error" not in mem:
        lines.append("### 记忆系统")
        lines.append(f"总记忆: {mem.get('total', 0)} 条")
        cats = mem.get("categories", {})
        if cats:
            parts = [f"{k} {v}" for k, v in cats.items()]
            lines.append(f"分类: {', '.join(parts)}")
        lines.append(f"KG: {mem.get('kg_entities', 0)} 实体, {mem.get('kg_active_triples', 0)} 三元组")
        lines.append(f"回收站: {mem.get('trash_count', 0)} 条\n")
    elif isinstance(mem, dict):
        lines.append(f"### 记忆系统\n❌ {mem.get('error', 'unknown error')}\n")

    # ── Runtime ──
    rt = data.get("runtime")
    if isinstance(rt, dict) and "error" not in rt:
        lines.append("### 运行状态")
        err_count = rt.get("error_count_24h", 0)
        lines.append(f"24h 错误: {err_count} 条")
        maint = rt.get("last_maintenance")
        if maint:
            lines.append(f"上次维护: {maint}")
        else:
            lines.append("上次维护: 暂无记录")
        lines.append("")
    elif isinstance(rt, dict):
        lines.append(f"### 运行状态\n❌ {rt.get('error', 'unknown error')}\n")

    lines.append(f"_检查时间: {data.get('checked_at', '?')}_")
    return "\n".join(lines)


class CheckupSkill(Skill):

    async def execute(self, context: SkillContext) -> SkillResult:
        if context.tool_name != "run_checkup":
            return SkillResult(output=f"Unknown tool: {context.tool_name}", success=False)

        try:
            from mochi.checkup_core import run_checkup
            from mochi.config import OWNER_USER_ID
            uid = context.user_id or OWNER_USER_ID
            data = run_checkup(uid)
        except Exception as e:
            log.error("Checkup failed: %s", e, exc_info=True)
            return SkillResult(output=f"体检失败: {e}", success=False)

        return SkillResult(output=_format_markdown(data))
