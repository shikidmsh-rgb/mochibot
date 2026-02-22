"""Memory engine — extract, deduplicate, and maintain memories.

Three-layer memory architecture:
  Layer 3: Raw conversation history   (messages table)
  Layer 2: Extracted memory items     (memory_items table — facts, preferences, events)
  Layer 1: Core memory summary        (core_memory table — compact, always in system prompt)

Core memory is owned by the chat model — it updates core memory during
conversation via the memory skill. Maintenance only audits token budget.

Nightly cycle: extract → deduplicate → audit core token count.
"""

import json
import logging
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from mochi.config import (
    CORE_MEMORY_MAX_TOKENS,
    COMPRESS_DAILY_AFTER_DAYS,
    COMPRESS_WEEKLY_AFTER_DAYS,
    TRASH_PURGE_DAYS,
    TIMEZONE_OFFSET_HOURS,
    OWNER_USER_ID,
)
from mochi.llm import get_client
from mochi.prompt_loader import get_prompt
from mochi.db import (
    get_core_memory, update_core_memory,
    save_memory_item, recall_memory,
    get_unprocessed_conversations, mark_messages_processed,
    get_all_memory_items, delete_memory_items, merge_memory_items,
    log_usage,
)

log = logging.getLogger(__name__)

TZ = timezone(timedelta(hours=TIMEZONE_OFFSET_HOURS))


# ═══════════════════════════════════════════════════════════════════════════
# Memory Extraction (Layer 3 → Layer 2)
# ═══════════════════════════════════════════════════════════════════════════

def extract_memories(user_id: int = 0) -> int:
    """Extract memory items from unprocessed conversations.

    Uses LLM to identify facts, preferences, events worth remembering.
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

    client = get_client(purpose="think")
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

    # Parse extracted memories (expects JSON array)
    count = 0
    try:
        memories = json.loads(response.content)
        if isinstance(memories, list):
            for mem in memories:
                if isinstance(mem, dict) and "content" in mem:
                    save_memory_item(
                        uid,
                        category=mem.get("category", "general"),
                        content=mem["content"],
                        importance=mem.get("importance", 1),
                        source="extracted",
                    )
                    count += 1
    except json.JSONDecodeError:
        log.warning("Failed to parse memory extraction result")

    # Mark conversations as processed
    if conversations:
        mark_messages_processed(uid, conversations[-1]["id"])

    log.info("Extracted %d memories from %d messages", count, len(conversations))
    return count


# ═══════════════════════════════════════════════════════════════════════════
# Memory Deduplication
# ═══════════════════════════════════════════════════════════════════════════

DEDUP_PROMPT = """You are a memory maintenance system. Analyze the memory items below and identify duplicates or near-duplicates that should be merged.

## Rules
- Items in the SAME category with very similar content should be merged
- Keep the HIGHEST importance level when merging
- If items are related but NOT duplicates (different facts), DON'T merge
- Be conservative: when in doubt, DON'T merge

## Output Format
Return a JSON array of merge operations:
[{"keep": <id_to_keep>, "delete": [<ids_to_delete>], "merged_content": "combined text"}]

Return empty array [] if no merges needed.

## Memory Items
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
    client = get_client(purpose="think")

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

        try:
            merges = json.loads(response.content)
            if isinstance(merges, list):
                for op in merges:
                    if "keep" in op and "delete" in op and "merged_content" in op:
                        merge_memory_items(op["keep"], op["delete"], op["merged_content"])
                        total_merged += len(op["delete"])
        except json.JSONDecodeError:
            log.warning("Failed to parse dedup result for category: %s", cat)

    log.info("Deduplicated %d memory items", total_merged)
    return total_merged


# ═══════════════════════════════════════════════════════════════════════════
# Core Memory Rebuild (Layer 2 → Layer 1)
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
        log.info("Core memory for user %d: %d tokens (budget %d) ✓",
                 uid, token_count, CORE_MEMORY_MAX_TOKENS)

    return {"status": "over_budget" if over else "ok", "tokens": token_count, "over_budget": over}


# ═══════════════════════════════════════════════════════════════════════════
# Smart Maintenance (nightly orchestrator)
# ═══════════════════════════════════════════════════════════════════════════

def smart_maintenance(user_id: int = 0) -> dict:
    """Run full memory maintenance cycle: extract → dedup → audit core.

    Called nightly by the maintenance scheduler.
    Core memory is NOT rebuilt here — chat model owns that content.
    We only audit the token budget.
    """
    uid = user_id or OWNER_USER_ID
    log.info("Starting smart maintenance for user %d", uid)

    results = {
        "extracted": 0,
        "deduplicated": 0,
        "core_audit": {},
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
        results["core_audit"] = audit_core_memory_tokens(uid)
    except Exception as e:
        log.error("Core memory audit failed: %s", e)

    log.info("Maintenance complete: %s", results)
    return results
