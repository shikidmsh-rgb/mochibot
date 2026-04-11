"""Meal skill handler — meal logging, querying, and deletion.

Tool-only mode:
- log_meal: record meals with structured nutrition data
- query_meals: query meal history with daily summaries
- delete_meal: remove incorrect meal records
"""

import json
import logging
from datetime import datetime

from mochi.config import TZ, logical_today
from mochi.skills.base import Skill, SkillContext, SkillResult
from mochi.skills.meal.queries import MEAL_LABELS, VALID_MEAL_TYPES
from mochi.db import save_health_log, query_health_log, delete_health_log_items

log = logging.getLogger(__name__)


class MealSkill(Skill):

    async def execute(self, context: SkillContext) -> SkillResult:
        args = context.args
        tool = context.tool_name
        uid = context.user_id

        if tool == "log_meal":
            return self._log_meal(uid, args)
        elif tool == "query_meals":
            return self._query_meals(uid, args)
        elif tool == "delete_meal":
            return self._delete_meal(uid, args)
        return SkillResult(output=f"Unknown meal tool: {tool}", success=False)

    # ── Helpers ──────────────────────────────────────────────

    def _normalize_meal_items(self, raw_items: str | list) -> list[dict]:
        """Parse and normalize meal items from LLM output.

        Ensures every item has: name, calories, protein_g, carbs_g, fat_g.
        Missing numeric fields default to 0.
        """
        if isinstance(raw_items, str):
            try:
                items = json.loads(raw_items)
            except (json.JSONDecodeError, TypeError):
                return []
        else:
            items = raw_items

        if not isinstance(items, list):
            return []

        normalized = []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            normalized.append({
                "name": name,
                "calories": int(item.get("calories", 0)),
                "protein_g": round(float(item.get("protein_g", 0)), 1),
                "carbs_g": round(float(item.get("carbs_g", 0)), 1),
                "fat_g": round(float(item.get("fat_g", 0)), 1),
            })
        return normalized

    # ── Tool implementations ─────────────────────────────────

    def _log_meal(self, user_id: int, args: dict) -> SkillResult:
        """Record a meal with structured nutrition data."""
        meal_type = args.get("meal_type", "").strip().lower()
        if meal_type not in VALID_MEAL_TYPES:
            return SkillResult(
                output=f"Error: meal_type must be one of: {', '.join(sorted(VALID_MEAL_TYPES))}",
                success=False,
            )

        items = self._normalize_meal_items(args.get("items", "[]"))
        if not items:
            return SkillResult(
                output="Error: items must be a non-empty JSON array of food items.",
                success=False,
            )

        total_calories = int(args.get("total_calories", 0))
        total_protein = round(float(args.get("total_protein_g", 0)), 1)
        total_carbs = round(float(args.get("total_carbs_g", 0)), 1)
        total_fat = round(float(args.get("total_fat_g", 0)), 1)
        source_type = args.get("source", "text").strip().lower()
        date_str = args.get("date", "").strip()

        if not date_str:
            date_str = logical_today()
        else:
            try:
                datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                return SkillResult(
                    output=f"Error: invalid date format '{date_str}'. Use YYYY-MM-DD.",
                    success=False,
                )

        # Build structured metrics JSON
        metrics = {
            "meal_type": meal_type,
            "items": items,
            "total": {
                "calories": total_calories,
                "protein_g": total_protein,
                "carbs_g": total_carbs,
                "fat_g": total_fat,
            },
            "source": source_type,
        }

        # Build human-readable content summary
        item_names = "+".join(it["name"] for it in items[:4])
        if len(items) > 4:
            item_names += f"等{len(items)}项"
        label = MEAL_LABELS.get(meal_type, meal_type)
        content = (
            f"[{date_str}] {label}: {item_names} "
            f"~{total_calories}kcal (P{total_protein:.0f}/C{total_carbs:.0f}/F{total_fat:.0f}g)"
        )

        # Use source="meal_{type}" so each meal_type gets its own upsert slot per day.
        # Snacks append timestamp to allow multiple per day.
        db_source = f"meal_{meal_type}"
        if meal_type == "snack":
            ts = datetime.now(TZ).strftime("%H%M%S")
            db_source = f"meal_snack_{ts}"

        mid = save_health_log(
            user_id=user_id,
            date=date_str,
            log_type="meal",
            content=content,
            source=db_source,
            metrics=json.dumps(metrics, ensure_ascii=False),
            importance=1,
        )

        log.info("Meal logged: #%d [%s] %s", mid, meal_type, content[:80])

        return SkillResult(
            output=(
                f"✅ 已记录{label}: {item_names} ~{total_calories}kcal "
                f"(蛋白质{total_protein:.0f}g/碳水{total_carbs:.0f}g/脂肪{total_fat:.0f}g)"
            ),
        )

    def _query_meals(self, user_id: int, args: dict) -> SkillResult:
        """Query meal history with daily nutrition totals."""
        date_str = args.get("date", "").strip()
        days = int(args.get("days", 1))

        if date_str:
            try:
                datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                return SkillResult(
                    output=f"Error: invalid date format '{date_str}'. Use YYYY-MM-DD.",
                    success=False,
                )

        records = query_health_log(
            user_id=user_id,
            types=["meal"],
            days=days,
            date=date_str or None,
        )

        if not records:
            period = date_str if date_str else f"最近{days}天"
            return SkillResult(output=f"{period}无饮食记录")

        # Group by date
        by_date: dict[str, list[dict]] = {}
        for r in records:
            by_date.setdefault(r["date"], []).append(r)

        lines = []
        for date, day_records in sorted(by_date.items()):
            day_total_cal = 0
            day_total_p = 0.0
            day_total_c = 0.0
            day_total_f = 0.0
            meal_parts = []

            for r in day_records:
                try:
                    m = json.loads(r.get("metrics") or "{}")
                except (json.JSONDecodeError, TypeError):
                    m = {}

                mt = m.get("meal_type", "?")
                label = MEAL_LABELS.get(mt, mt)
                total = m.get("total", {})
                cal = total.get("calories", 0)
                p = total.get("protein_g", 0)
                c = total.get("carbs_g", 0)
                f = total.get("fat_g", 0)

                item_names = ", ".join(
                    it.get("name", "?") for it in m.get("items", [])[:4]
                )
                meal_parts.append(
                    f"  {label}: {item_names} ~{cal}kcal (P{p:.0f}/C{c:.0f}/F{f:.0f}g)"
                )

                day_total_cal += cal
                day_total_p += p
                day_total_c += c
                day_total_f += f

            lines.append(f"📅 {date}")
            lines.extend(meal_parts)
            lines.append(
                f"  ── 日合计: {day_total_cal}kcal | "
                f"蛋白质{day_total_p:.0f}g 碳水{day_total_c:.0f}g 脂肪{day_total_f:.0f}g"
            )

        return SkillResult(output="\n".join(lines))

    def _delete_meal(self, user_id: int, args: dict) -> SkillResult:
        """Delete a meal record by date and meal_type."""
        meal_type = args.get("meal_type", "").strip().lower()
        if meal_type not in VALID_MEAL_TYPES:
            return SkillResult(
                output=f"Error: meal_type must be one of: {', '.join(sorted(VALID_MEAL_TYPES))}",
                success=False,
            )

        date_str = args.get("date", "").strip()
        if not date_str:
            date_str = logical_today()

        # Query only the target date
        records = query_health_log(
            user_id=user_id,
            types=["meal"],
            date=date_str,
        )
        to_delete = []
        for r in records:
            try:
                m = json.loads(r.get("metrics") or "{}")
            except (json.JSONDecodeError, TypeError):
                m = {}
            if m.get("meal_type") == meal_type:
                to_delete.append(r["id"])

        label = MEAL_LABELS.get(meal_type, meal_type)
        if not to_delete:
            return SkillResult(output=f"{date_str} 没有找到{label}记录")

        deleted = delete_health_log_items(to_delete)
        log.info("Meal deleted: %d records [%s] on %s", deleted, meal_type, date_str)
        return SkillResult(output=f"✅ 已删除 {date_str} 的{label}记录 ({deleted}条)")
