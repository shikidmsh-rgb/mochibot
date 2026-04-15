"""Memory engine — extract, deduplicate, and maintain memories.

Three-layer memory architecture:
  Layer 3: Raw conversation history   (messages table)
  Layer 2: Extracted memory items     (memory_items table — facts, preferences, events)
  Layer 1: Core memory summary        (core_memory table — compact, always in system prompt)

Core memory is primarily owned by the chat model — it updates core memory
during conversations via the memory skill. One exception: memories with
category "关系" (relational) are auto-appended to core memory during
nightly extraction, as these capture relationship dynamics that should
always be available in context.

Nightly cycle: extract → deduplicate → outdated → salience → audit core → trash purge.
"""

import json
import logging
import re
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from mochi.config import (
    CORE_MEMORY_MAX_TOKENS,
    COMPRESS_DAILY_AFTER_DAYS,
    COMPRESS_WEEKLY_AFTER_DAYS,
    TRASH_PURGE_DAYS,
    TIMEZONE_OFFSET_HOURS,
    OWNER_USER_ID,
    MEMORY_DEMOTE_AFTER_DAYS,
    MEMORY_DEMOTE_MIN_ACCESS,
)
from mochi.llm import get_client_for_tier
from mochi.prompt_loader import get_prompt
from mochi.db import (
    get_core_memory, update_core_memory,
    save_memory_item, recall_memory,
    get_unprocessed_conversations, mark_messages_processed,
    get_all_memory_items, delete_memory_items, merge_memory_items,
    update_memory_importance, cleanup_old_trash,
    log_usage,
)

log = logging.getLogger(__name__)

TZ = timezone(timedelta(hours=TIMEZONE_OFFSET_HOURS))

# Max relational items auto-appended to core_memory per extraction cycle
_MAX_RELATIONAL_PER_CYCLE = 3
# Stop auto-appending when core memory exceeds this token count (leave room for manual edits)
_RELATIONAL_TOKEN_BUDGET = 700


# ═══════════════════════════════════════════════════════════════════════════
# JSON Parsing Helper
# ═══════════════════════════════════════════════════════════════════════════

def _parse_gpt_json(raw: str) -> dict | list:
    """Robustly parse JSON from LLM output.

    Handles markdown fences, trailing commas, and stray text around JSON.
    """
    if not raw:
        return {}

    # Strip markdown code fences
    cleaned = re.sub(r"^```(?:json)?\s*\n?", "", raw.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"\n?```\s*$", "", cleaned.strip(), flags=re.MULTILINE)

    # Try direct parse first
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Remove trailing commas before } or ]
    no_trailing = re.sub(r",\s*([}\]])", r"\1", cleaned)
    try:
        return json.loads(no_trailing)
    except json.JSONDecodeError:
        pass

    # Try to extract JSON object or array from surrounding text
    for pattern in [r"\{[\s\S]*\}", r"\[[\s\S]*\]"]:
        match = re.search(pattern, cleaned)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                # Try with trailing comma fix
                candidate = re.sub(r",\s*([}\]])", r"\1", match.group())
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    pass

    log.warning("Failed to parse JSON from LLM output: %s", raw[:200])
    return {}


# ═══════════════════════════════════════════════════════════════════════════
# Memory Extraction (Layer 3 → Layer 2)
# ═══════════════════════════════════════════════════════════════════════════

def extract_memories(user_id: int = 0) -> int:
    """Extract memory items from unprocessed conversations.

    Uses LLM to identify facts, preferences, events worth remembering.
    Items with category "关系" (relational) are also auto-appended to
    core_memory — see module docstring for rationale.

    Returns number of memories extracted.
    """
    uid = user_id or OWNER_USER_ID
    conversations = get_unprocessed_conversations(uid)
    if not conversations:
        return 0

    # Build conversation text for LLM
    conv_text = "\n".join(
        f"[{m['created_at']}] {m['role']}: {m['content']}"
        for m in conversations
    )

    prompt = get_prompt("memory_extract")
    if not prompt:
        log.warning("memory_extract prompt not found, skipping extraction")
        return 0

    client = get_client_for_tier("deep")
    response = client.chat(
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": conv_text},
        ],
        temperature=0.3,
        max_tokens=1024,
    )

    log_usage(
        response.prompt_tokens, response.completion_tokens,
        response.total_tokens, model=response.model, purpose="memory_extract",
    )

    # Generate embeddings for vector search
    try:
        from mochi.model_pool import get_pool
        pool = get_pool()
    except Exception:
        pool = None

    # Parse extracted memories (expects JSON array)
    count = 0
    relational_items: list[str] = []
    parsed = _parse_gpt_json(response.content)
    memories = parsed if isinstance(parsed, list) else parsed.get("memories", [])
    for mem in memories:
        if isinstance(mem, dict) and "content" in mem:
            category = mem.get("category", "其他")
            embedding = None
            if pool:
                try:
                    embedding = pool.embed(mem["content"])
                except Exception as e:
                    log.warning("Embedding failed for memory: %s", e)
            save_memory_item(
                uid,
                category=category,
                content=mem["content"],
                importance=mem.get("importance", 1),
                source="extracted",
                embedding=embedding,
            )
            count += 1
            if category == "关系":
                relational_items.append(mem["content"])

    # Auto-append relational items to core_memory
    if relational_items:
        _append_relational_to_core(uid, relational_items)

    # Mark conversations as processed
    if conversations:
        mark_messages_processed(uid, conversations[-1]["id"])

    log.info("Extracted %d memories from %d messages", count, len(conversations))
    return count


def _append_relational_to_core(user_id: int, items: list[str]) -> None:
    """Append relational memory items to core_memory with safety guards.

    Guards:
    - Max _MAX_RELATIONAL_PER_CYCLE items per call
    - Skips if core_memory already exceeds _RELATIONAL_TOKEN_BUDGET tokens
    - Deduplicates against existing core_memory lines
    """
    import tiktoken

    current_core = get_core_memory(user_id) or ""

    # Token budget check
    if current_core.strip():
        enc = tiktoken.encoding_for_model("gpt-4o")
        token_count = len(enc.encode(current_core))
        if token_count >= _RELATIONAL_TOKEN_BUDGET:
            log.warning(
                "Core memory at %d tokens (budget %d), skipping relational auto-append",
                token_count, _RELATIONAL_TOKEN_BUDGET,
            )
            return

    # Build set of existing lines for dedup (strip "- " prefix, lowercase)
    existing_lines = {
        line.lstrip("- ").strip().lower()
        for line in current_core.split("\n")
        if line.strip()
    }

    # Filter new items: dedup + cap
    new_lines = []
    for content in items[:_MAX_RELATIONAL_PER_CYCLE]:
        if content.strip().lower() not in existing_lines:
            new_lines.append(f"- {content}")

    if not new_lines:
        return

    updated = (
        current_core.rstrip() + "\n" + "\n".join(new_lines)
        if current_core.strip() else "\n".join(new_lines)
    )
    update_core_memory(user_id, updated)
    log.info("Auto-appended %d relational item(s) to core_memory", len(new_lines))


# ═══════════════════════════════════════════════════════════════════════════
# Memory Deduplication
# ═══════════════════════════════════════════════════════════════════════════

DEDUP_PROMPT = """你是一个记忆维护系统。分析以下记忆条目，找出应该合并的重复或近似重复项。

## 规则
- 同一类别中内容高度相似的条目应合并
- 合并时保留最高的重要级别
- 有关联但不是重复的条目（不同事实）不要合并
- 保守判断：不确定时不合并

## 输出格式
返回 JSON 对象：
{"operations": [{"keep": <保留的id>, "delete": [<删除的id列表>], "merged_content": "合并后的文本", "importance": <最高重要级别>}]}

无需合并时返回：{"operations": []}

## 记忆条目
"""


def deduplicate_memories(user_id: int = 0) -> int:
    """Find and merge duplicate/near-duplicate memories. Returns merge count."""
    uid = user_id or OWNER_USER_ID
    items = get_all_memory_items(uid)
    if len(items) < 5:
        return 0

    # Group by category for efficiency
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        by_cat[item["category"]].append(item)

    total_merged = 0
    client = get_client_for_tier("deep")

    for cat, cat_items in by_cat.items():
        if len(cat_items) < 2:
            continue

        items_text = "\n".join(
            f"[id={m['id']}] (importance={m['importance']}) {m['content']}"
            for m in cat_items
        )

        response = client.chat(
            messages=[
                {"role": "system", "content": DEDUP_PROMPT},
                {"role": "user", "content": f"Category: {cat}\n\n{items_text}"},
            ],
            temperature=0.2,
            max_tokens=1024,
        )

        log_usage(
            response.prompt_tokens, response.completion_tokens,
            response.total_tokens, model=response.model, purpose="memory_dedup",
        )

        parsed = _parse_gpt_json(response.content)
        operations = parsed.get("operations", []) if isinstance(parsed, dict) else parsed
        if isinstance(operations, list):
            for op in operations:
                if "keep" in op and "delete" in op and "merged_content" in op:
                    merge_memory_items(
                        op["keep"], op["delete"], op["merged_content"],
                        new_importance=op.get("importance"),
                    )
                    total_merged += len(op["delete"])

    log.info("Deduplicated %d memory items", total_merged)
    return total_merged


# ═══════════════════════════════════════════════════════════════════════════
# Outdated Memory Removal
# ═══════════════════════════════════════════════════════════════════════════

MEMORY_OUTDATED_PROMPT = """你是一个记忆维护系统。分析以下记忆条目，找出已过时应删除的条目。

## 当前日期
{current_date}

## 过时判断规则
- **已过期的事件**："下周有会议"但已过去数周 → 删除
- **已解决的问题**："感冒了"但那是3个月前且无后续 → 删除
- **临时情绪**：2个月前的"今天心情不好" → 删除（除非是反复出现的模式）
- **不要删除**：慢性病情、偏好、长期事实、反复出现的模式
- 保守判断：不确定时保留

## 输出格式
返回 JSON 对象：
{{"operations": [
  {{
    "item_id": 123,
    "action": "delete",
    "reason": "删除原因简述"
  }}
]}}

无过时内容时返回：{{"operations": []}}
"""


def remove_outdated_memories(user_id: int = 0) -> dict:
    """Use LLM to identify outdated memories and soft-delete them.

    Batches items into LLM calls (200 per batch) to minimize API usage.
    Returns {deleted, errors}.
    """
    uid = user_id or OWNER_USER_ID
    items = get_all_memory_items(uid)
    if not items:
        return {"deleted": 0, "errors": 0}

    current_date = datetime.now(TZ).strftime("%Y-%m-%d %A")
    all_delete_ids = []
    errors = 0
    BATCH_SIZE = 200

    client = get_client_for_tier("deep")

    for i in range(0, len(items), BATCH_SIZE):
        batch = items[i:i + BATCH_SIZE]
        items_text = "\n".join(
            f"ID:{item['id']} | [{item['category']}] ★{item['importance']} | {item['content']} "
            f"(created: {item['created_at'][:10]}, updated: {item['updated_at'][:10]})"
            for item in batch
        )

        try:
            response = client.chat(
                messages=[
                    {"role": "system", "content": MEMORY_OUTDATED_PROMPT.format(current_date=current_date)},
                    {"role": "user", "content": f"## Memory Items (batch {i // BATCH_SIZE + 1}):\n{items_text}"},
                ],
                temperature=0.2,
                max_tokens=1024,
            )

            log_usage(
                response.prompt_tokens, response.completion_tokens,
                response.total_tokens, model=response.model, purpose="memory_outdated",
            )

            parsed = _parse_gpt_json(response.content)
            operations = parsed.get("operations", []) if isinstance(parsed, dict) else parsed
            if isinstance(operations, list):
                for op in operations:
                    if isinstance(op, dict) and op.get("action") == "delete" and "item_id" in op:
                        all_delete_ids.append(op["item_id"])

        except Exception as e:
            log.warning("Outdated memory detection failed for batch %d: %s",
                        i // BATCH_SIZE + 1, e)
            errors += 1

    # Batch delete all identified items
    deleted = 0
    if all_delete_ids:
        deleted = delete_memory_items(all_delete_ids, deleted_by="maintenance")

    log.info("Outdated removal: deleted %d, errors %d", deleted, errors)
    return {"deleted": deleted, "errors": errors}


# ═══════════════════════════════════════════════════════════════════════════
# Salience Rebalancing
# ═══════════════════════════════════════════════════════════════════════════

SALIENCE_PROMPT = """你是一个记忆重要度评估器。审查以下候选记忆，根据对话中的访问频率判断是否需要调整重要度。

## 当前日期
{current_date}

## 规则
- 你收到的是经过规则预筛选的候选记忆
- **提升候选**（importance 1→2）：这些是频繁被访问但当前评级为日常（★1）的记忆。如果主题确实对用户重要（反复提及=真正关心），则提升；如果只是背景噪音（如天气查询、日常问候），则不提升
- **降级候选**（importance 2→1）：这些是评级为重要（★2）但长期未被访问的记忆。仅在主题确实被放弃时降级；如果是稳定的长期事实（健康状况、偏好等），则不降级
- **绝不触碰** importance=3 的条目（关键，人工指定）
- 保守判断：不确定时不修改
- 综合考虑内容语义，不只看数字

## 输出格式 (JSON)
{{"operations": [
  {{
    "item_id": 123,
    "action": "promote",
    "new_importance": 2,
    "reason": "原因简述"
  }}
]}}

无需修改时返回：{{"operations": []}}
"""


def _find_promote_candidates(user_id: int) -> list[dict]:
    """Find memories with importance=1 but high access_count (frequently mentioned)."""
    items = get_all_memory_items(user_id)
    return [
        item for item in items
        if item["importance"] == 1
        and item.get("access_count", 0) >= MEMORY_DEMOTE_MIN_ACCESS
    ]


def _find_demote_candidates(user_id: int) -> list[dict]:
    """Find memories with importance=2 but not accessed for a long time.

    Never touches importance=3 (critical).
    """
    items = get_all_memory_items(user_id)
    cutoff = datetime.now(TZ) - timedelta(days=MEMORY_DEMOTE_AFTER_DAYS)
    candidates = []
    for item in items:
        if item["importance"] != 2:
            continue
        if item.get("access_count", 0) >= MEMORY_DEMOTE_MIN_ACCESS:
            continue
        try:
            last_acc = datetime.fromisoformat(item["last_accessed"])
            if last_acc.tzinfo is None:
                last_acc = last_acc.replace(tzinfo=TZ)
            if last_acc > cutoff:
                continue
        except (ValueError, TypeError, KeyError):
            # No valid last_accessed → treat as ancient, include as candidate
            pass
        candidates.append(item)
    return candidates


def rebalance_salience(user_id: int = 0) -> dict:
    """Rebalance memory importance based on access patterns.

    Uses rule-based pre-filtering + LLM confirmation.
    - Promote: importance 1→2 for frequently accessed memories
    - Demote: importance 2→1 for abandoned memories
    - Never touches importance=3

    Returns {promoted, demoted}.
    """
    uid = user_id or OWNER_USER_ID
    promote_candidates = _find_promote_candidates(uid)
    demote_candidates = _find_demote_candidates(uid)

    all_candidates = promote_candidates + demote_candidates
    if not all_candidates:
        return {"promoted": 0, "demoted": 0}

    # Format candidates for LLM
    candidate_lines = []
    for item in promote_candidates:
        candidate_lines.append(
            f"PROMOTE? ID:{item['id']} | ★{item['importance']} | "
            f"access:{item.get('access_count', 0)} | [{item['category']}] "
            f"{item['content'][:120]} "
            f"(created: {item['created_at'][:10]}, updated: {item['updated_at'][:10]})"
        )
    for item in demote_candidates:
        last_acc = item.get("last_accessed", "N/A")
        if last_acc:
            last_acc = last_acc[:10]
        else:
            last_acc = "N/A"
        candidate_lines.append(
            f"DEMOTE? ID:{item['id']} | ★{item['importance']} | "
            f"access:{item.get('access_count', 0)} | [{item['category']}] "
            f"{item['content'][:120]} "
            f"(created: {item['created_at'][:10]}, last_accessed: {last_acc})"
        )

    current_date = datetime.now(TZ).strftime("%Y-%m-%d %A")

    try:
        client = get_client_for_tier("deep")
        response = client.chat(
            messages=[
                {"role": "system", "content": SALIENCE_PROMPT.format(current_date=current_date)},
                {"role": "user", "content": "## 候选记忆:\n" + "\n".join(candidate_lines)},
            ],
            temperature=0.2,
            max_tokens=512,
        )

        log_usage(
            response.prompt_tokens, response.completion_tokens,
            response.total_tokens, model=response.model, purpose="salience_rebalance",
        )

        parsed = _parse_gpt_json(response.content)
        operations = parsed.get("operations", []) if isinstance(parsed, dict) else parsed
        if not isinstance(operations, list):
            operations = []

    except Exception as e:
        log.warning("Salience rebalance LLM call failed: %s", e)
        return {"promoted": 0, "demoted": 0}

    promoted = 0
    demoted = 0

    for op in operations:
        try:
            item_id = op["item_id"]
            new_imp = op["new_importance"]
            action = op.get("action", "")

            # Safety: never set importance to 3 via salience
            if new_imp >= 3:
                continue
            # Safety: only allow 1→2 (promote) or 2→1 (demote)
            if new_imp not in (1, 2):
                continue

            update_memory_importance(item_id, new_imp)

            if action == "promote":
                promoted += 1
                log.info("Salience promote: ID %d → ★%d (%s)", item_id, new_imp, op.get("reason", ""))
            elif action == "demote":
                demoted += 1
                log.info("Salience demote: ID %d → ★%d (%s)", item_id, new_imp, op.get("reason", ""))
        except Exception as e:
            log.warning("Salience rebalance failed for op %s: %s", op, e)

    log.info("Salience rebalance: promoted %d, demoted %d (from %d candidates)",
             promoted, demoted, len(all_candidates))
    return {"promoted": promoted, "demoted": demoted}


# ═══════════════════════════════════════════════════════════════════════════
# Core Memory Audit (Layer 2 → Layer 1)
# ═══════════════════════════════════════════════════════════════════════════

def audit_core_memory_tokens(user_id: int = 0) -> dict:
    """Check core memory token count. Returns status + token count.

    Core memory content is owned by the chat model — it updates core
    memory during conversations via the memory skill. This function
    only audits whether it's within the token budget.
    """
    import tiktoken
    uid = user_id or OWNER_USER_ID
    content = get_core_memory(uid) or ""
    if not content.strip():
        return {"status": "empty", "tokens": 0, "over_budget": False}

    enc = tiktoken.encoding_for_model("gpt-4o")
    token_count = len(enc.encode(content))
    over = token_count > CORE_MEMORY_MAX_TOKENS

    if over:
        log.warning("Core memory for user %d is %d tokens (budget %d) — needs trimming",
                    uid, token_count, CORE_MEMORY_MAX_TOKENS)
    else:
        log.info("Core memory for user %d: %d tokens (budget %d) OK",
                 uid, token_count, CORE_MEMORY_MAX_TOKENS)

    return {"status": "over_budget" if over else "ok", "tokens": token_count, "over_budget": over}


# ═══════════════════════════════════════════════════════════════════════════
# Smart Maintenance (nightly orchestrator)
# ═══════════════════════════════════════════════════════════════════════════

def smart_maintenance(user_id: int = 0) -> dict:
    """Run full memory maintenance cycle.

    Called nightly by the maintenance scheduler.
    Order: extract → dedup → outdated → salience → core audit → trash purge.
    """
    uid = user_id or OWNER_USER_ID
    log.info("Starting smart maintenance for user %d", uid)

    results = {
        "extracted": 0,
        "deduplicated": 0,
        "outdated": {},
        "salience": {},
        "core_audit": {},
        "trash_purged": 0,
    }

    try:
        results["extracted"] = extract_memories(uid)
    except Exception as e:
        log.error("Memory extraction failed: %s", e)

    try:
        results["deduplicated"] = deduplicate_memories(uid)
    except Exception as e:
        log.error("Memory dedup failed: %s", e)

    try:
        results["outdated"] = remove_outdated_memories(uid)
    except Exception as e:
        log.error("Outdated removal failed: %s", e)

    try:
        results["salience"] = rebalance_salience(uid)
    except Exception as e:
        log.error("Salience rebalance failed: %s", e)

    try:
        results["core_audit"] = audit_core_memory_tokens(uid)
    except Exception as e:
        log.error("Core memory audit failed: %s", e)

    try:
        results["trash_purged"] = cleanup_old_trash(TRASH_PURGE_DAYS)
    except Exception as e:
        log.error("Trash purge failed: %s", e)

    log.info("Maintenance complete: %s", results)
    return results


# ═══════════════════════════════════════════════════════════════════════════
# Knowledge Graph Extraction (conversation → KG triples)
# ═══════════════════════════════════════════════════════════════════════════

def extract_kg(user_id: int = 0) -> dict:
    """Extract named entities and relationships from unprocessed conversations.

    Uses lite-tier LLM to identify entities (people, pets, places) and factual
    relationships as subject-predicate-object triples. Designed for nightly
    maintenance — not called during chat turns.

    Returns {"entities": int, "triples": int} or {} on skip/error.
    """
    from mochi.config import KG_ENABLED
    if not KG_ENABLED:
        return {}

    uid = user_id or OWNER_USER_ID
    conversations = get_unprocessed_conversations(uid)
    if not conversations:
        return {"entities": 0, "triples": 0}

    conv_text = "\n".join(
        f"{m['role'].capitalize()}: {m['content']}"
        for m in conversations
        if m.get("content")
    )
    if len(conv_text) < 50:
        return {"entities": 0, "triples": 0}

    # Build known-entities context for the prompt
    from mochi.knowledge_graph import (
        list_entities, get_or_create_entity, add_triple,
    )
    known = list_entities(uid)[:50]
    known_text = ", ".join(
        f"{e['display_name']}({e['entity_type']})" for e in known
    ) if known else "(none yet)"

    prompt_template = get_prompt("kg_extract")
    if not prompt_template:
        log.warning("kg_extract prompt not found, skipping KG extraction")
        return {}
    prompt = prompt_template.replace("{{known_entities}}", known_text)

    try:
        client = get_client_for_tier("lite")
        response = client.chat(
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": conv_text},
            ],
            temperature=0.2,
            max_tokens=200,
        )
        log_usage(
            response.prompt_tokens, response.completion_tokens,
            response.total_tokens, model=response.model, purpose="kg_extract",
        )
    except Exception as e:
        log.error("KG extraction LLM call failed: %s", e)
        return {}

    parsed = _parse_gpt_json(response.content)
    if not isinstance(parsed, dict):
        return {}

    entity_count = 0
    triple_count = 0

    # Create entities
    raw_entities = parsed.get("entities", [])
    for ent in raw_entities:
        if not isinstance(ent, dict) or not ent.get("name"):
            continue
        try:
            get_or_create_entity(
                uid,
                name=ent["name"],
                entity_type=ent.get("type", "concept"),
            )
            entity_count += 1
        except Exception as e:
            log.warning("KG entity creation failed for %s: %s", ent.get("name"), e)

    # Create triples
    raw_triples = parsed.get("triples", [])
    for tri in raw_triples:
        if not isinstance(tri, dict):
            continue
        subj = tri.get("subject", "")
        pred = tri.get("predicate", "")
        obj = tri.get("object", "")
        if not (subj and pred and obj):
            continue
        try:
            subj_id = get_or_create_entity(uid, name=subj)
            obj_id = get_or_create_entity(uid, name=obj)
            add_triple(uid, subj_id, pred, obj_id, source="chat")
            triple_count += 1
        except Exception as e:
            log.warning("KG triple creation failed (%s→%s→%s): %s", subj, pred, obj, e)

    log.info("KG extraction: %d entities, %d triples from %d messages",
             entity_count, triple_count, len(conversations))
    return {"entities": entity_count, "triples": triple_count}

