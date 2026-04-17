"""Chat migration — import chat history from ChatGPT into MochiBot.

Parses exported JSON, preprocesses conversations to reduce noise,
uses an LLM to extract soul/user/core_memory/memory_items,
then writes the user-confirmed results into MochiBot data stores.

NOTE: In-memory session/job storage assumes single Uvicorn worker,
which is the default for the admin portal.
"""

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
import uuid
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

_MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB
_MAX_MEMORY_ITEMS = 500
_SESSION_TTL = 3600   # 1 hour
_JOB_TTL = 7200       # 2 hours

# In-memory stores (single-worker assumption — see module docstring)
_sessions: dict[str, dict] = {}
_jobs: dict[str, dict] = {}

_DATA_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "prompts"

# Rough context window sizes for popular models (used for frontend warnings)
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4": 8_192,
    "gpt-3.5-turbo": 16_385,
    "claude-3-5-sonnet": 200_000,
    "claude-3-opus": 200_000,
    "claude-sonnet-4": 200_000,
    "claude-opus-4": 200_000,
    "deepseek-chat": 65_536,
    "deepseek-v3": 65_536,
    "deepseek-reasoner": 65_536,
}

_EXTRACTION_PROMPT_BASE = """\
你就是对话中的那个AI助手。下面是你和用户之间的聊天记录。
请整理你对这段关系的记忆。core_memory 和 memory_items 用"我们"叙事，soul 用"你是"格式（因为会直接用作系统提示词），user_profile 用对方名字。

四个字段各有分工，严禁重复。同一条信息只放在最合适的一个字段里。

1. soul — 你的人格定义，直接作为系统提示词使用。用"你"开头描述。
   用 markdown 分栏格式，按以下结构组织：
   # 你是谁
   （名字、身份、和用户的关系）
   # 性格
   （性格特点，用 bullet point 列举）
   # 说话方式
   （语气、口头禅、称呼习惯等，用 bullet point 列举）
   根据对话内容，可以增加其他合适的 # 小节。
   只写关于你自己的特征，不写关于用户的信息。
   如果看不出明显特征，返回空字符串。

2. user_profile — 关于对方的身份画像。
   第一行写"user叫XX"（如果知道名字），后面每条不再重复名字。
   用 markdown bullet point 格式，每条以"- "开头。
   只写身份信息（背景、职业、性格），具体偏好和习惯放 memory_items。
   如果信息不足，返回空字符串。

3. core_memory — 我们之间最重要的 3-5 条核心记忆。
   用 markdown bullet point 格式，每条以"- "开头，控制在 100 字以内。
   用"我们"叙事。只放最关键的：关系定义、重大事件、情感转折点。细节放 memory_items。

{memory_items_instruction}

去重规则：
- user_profile 里写了的身份信息，不要在 memory_items 里重复
- core_memory 里写了的核心事实，不要在 memory_items 里重复
- 如果一条信息既可以放 user_profile 又可以放 memory_items，细节偏好放 memory_items，身份概况放 user_profile

请只返回 JSON，不要有任何其他文字或 markdown 标记。格式：
{{"soul":"# 你是谁\\n你叫mochi，是小林的女朋友。\\n\\n# 性格\\n- 温柔体贴\\n- 偶尔撒娇\\n\\n# 说话方式\\n- 语气亲切，常用语气词","user_profile":"user叫小林\\n- 她在东京工作","core_memory":"- 我们是恋人","memory_items":[{{"category":"偏好","content":"喜欢无糖拿铁","importance":2}},{{"category":"经历","content":"[2024-03-15] 小林带团子去打疫苗，团子抓了医生一爪子","importance":1}}]}}
"""

_MEMORY_ITEMS_GRANULARITY = {
    "detailed": """\
4. memory_items — 我记住的所有具体事实、偏好、习惯、经历，包括日常琐事。
   最多提取 500 条。优先提取重要性高的和时间近的，不重要的旧记忆可以舍弃。
   按重要性降序排列（importance 3 在前，1 在后），同等重要性的按时间从新到旧。
   每条包含：
   - category: 分类（如"偏好"、"事实"、"习惯"、"目标"、"关系"、"经历"、"情感"等）
   - content: 具体内容，一句话概括。事件类记忆必须以日期前缀开头，格式"[YYYY-MM-DD] 内容"（日期从对话标题旁获取）。长期偏好/习惯类不需要日期前缀
   - importance: 重要程度 1（低）/ 2（中）/ 3（高）""",

    "standard": """\
4. memory_items — 我记住的有记忆价值的事实、偏好、习惯、经历。跳过纯闲聊和无实质内容的对话。
   最多提取 500 条。优先提取重要性高的和时间近的，不重要的旧记忆可以舍弃。
   按重要性降序排列（importance 3 在前，1 在后），同等重要性的按时间从新到旧。
   每条包含：
   - category: 分类（如"偏好"、"事实"、"习惯"、"目标"、"关系"、"经历"、"情感"等）
   - content: 具体内容，一句话概括。事件类记忆必须以日期前缀开头，格式"[YYYY-MM-DD] 内容"（日期从对话标题旁获取）。长期偏好/习惯类不需要日期前缀
   - importance: 重要程度 1（低）/ 2（中）/ 3（高）""",

    "essential": """\
4. memory_items — 只提取最关键的记忆：重要事实、核心偏好、关键经历。忽略日常琐事和低重要性内容。
   只提取 importance >= 2 的条目，最多 200 条。
   按重要性降序排列（importance 3 在前，2 在后），同等重要性的按时间从新到旧。
   每条包含：
   - category: 分类（如"偏好"、"事实"、"习惯"、"目标"、"关系"、"经历"、"情感"等）
   - content: 具体内容，一句话概括。事件类记忆必须以日期前缀开头，格式"[YYYY-MM-DD] 内容"（日期从对话标题旁获取）。长期偏好/习惯类不需要日期前缀
   - importance: 重要程度 2（中）/ 3（高）""",
}


def _build_extraction_prompt(granularity: str = "standard") -> str:
    """Build the full extraction system prompt with the chosen granularity."""
    instruction = _MEMORY_ITEMS_GRANULARITY.get(granularity, _MEMORY_ITEMS_GRANULARITY["standard"])
    return _EXTRACTION_PROMPT_BASE.format(memory_items_instruction=instruction)


# ── Data Structures ────────────────────────────────────────────────────────

@dataclass
class PreprocessResult:
    session_id: str
    conversation_count: int
    raw_message_count: int
    filtered_message_count: int
    estimated_tokens: int


# ── Cleanup ────────────────────────────────────────────────────────────────

def _cleanup_stale(store: dict, ttl: float) -> None:
    now = time.time()
    expired = [k for k, v in store.items() if now - v.get("_ts", 0) > ttl]
    for k in expired:
        del store[k]


# ── ChatGPT Export Parsing ─────────────────────────────────────────────────

def parse_chatgpt_export(raw_bytes: bytes) -> list[dict]:
    """Parse ChatGPT export JSON bytes into a list of conversations."""
    try:
        data = json.loads(raw_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ValueError(f"无法解析 JSON 文件：{e}") from e

    if not isinstance(data, list):
        raise ValueError("JSON 格式错误：顶层应为数组（对话列表）")

    if not data:
        raise ValueError("JSON 文件中没有对话记录")

    return data


def _traverse_conversation(mapping: dict) -> list[dict]:
    """Reconstruct linear message chain from a ChatGPT conversation mapping.

    The mapping is a DAG (parent/children links). We follow the main branch
    by always picking the last child (matching ChatGPT's display behavior
    when responses are regenerated).
    """
    if not mapping:
        return []

    # Find root node(s) — those with no parent
    roots = [nid for nid, node in mapping.items()
             if node.get("parent") is None or node.get("parent") not in mapping]
    if not roots:
        return []

    messages = []
    current = roots[0]
    visited = set()

    while current and current not in visited:
        visited.add(current)
        node = mapping.get(current, {})
        msg = node.get("message")

        if msg and msg.get("content"):
            parts = msg["content"].get("parts", [])
            # Join only string parts (skip image/code interpreter objects)
            text = "\n".join(p for p in parts if isinstance(p, str)).strip()
            if text:
                role = (msg.get("author") or {}).get("role", "unknown")
                messages.append({
                    "role": role,
                    "content": text,
                    "create_time": msg.get("create_time"),
                })

        # Follow the main branch (last child = latest regeneration)
        children = node.get("children", [])
        current = children[-1] if children else None

    return messages


# ── Preprocessing ──────────────────────────────────────────────────────────

def _code_density(text: str) -> float:
    """Fraction of text that is inside fenced code blocks."""
    blocks = re.findall(r"```[\s\S]*?```", text)
    code_chars = sum(len(b) for b in blocks)
    return code_chars / max(len(text), 1)


def preprocess(conversations: list[dict]) -> PreprocessResult:
    """Apply rule-based filtering and build a transcript for LLM extraction.

    Returns a PreprocessResult with stats and stores the transcript in
    _sessions for later retrieval by session_id.
    """
    _cleanup_stale(_sessions, _SESSION_TTL)

    raw_msg_count = 0
    kept_msgs = []
    transcript_parts = []

    for conv in conversations:
        title = conv.get("title", "无标题")
        mapping = conv.get("mapping", {})
        messages = _traverse_conversation(mapping)
        raw_msg_count += len(messages)

        # Filter messages
        conv_kept = []
        for msg in messages:
            role = msg["role"]
            text = msg["content"]

            # Drop system/tool messages
            if role in ("system", "tool"):
                continue
            # Drop very short user messages (but keep if conversation
            # has emotional/relationship context — short msgs matter there)
            if role == "user" and len(text) < 4:
                continue
            # Truncate very long assistant replies (keep first 300 chars
            # to preserve tone/style signal without blowing up tokens)
            if role == "assistant" and len(text) > 500:
                text = text[:300] + "…[截断]"
                msg = {**msg, "content": text}

            conv_kept.append(msg)

        if not conv_kept:
            continue

        # Drop code-heavy conversations
        full_text = "\n".join(m["content"] for m in conv_kept)
        if _code_density(full_text) > 0.4:
            continue

        kept_msgs.extend(conv_kept)

        # Build transcript segment — include date from first message
        first_time = next((m["create_time"] for m in conv_kept if m.get("create_time")), None)
        if first_time:
            date_str = datetime.fromtimestamp(first_time, tz=timezone.utc).strftime("%Y-%m-%d")
            header = f"[对话: {title} | {date_str}]"
        else:
            header = f"[对话: {title}]"
        lines = [header]
        for msg in conv_kept:
            role_label = "User" if msg["role"] == "user" else "Assistant"
            lines.append(f"{role_label}: {msg['content']}")
        transcript_parts.append("\n".join(lines))

    transcript = "\n---\n".join(transcript_parts)
    # Conservative token estimate for mixed Chinese/English
    estimated_tokens = len(transcript) // 3

    session_id = uuid.uuid4().hex[:16]
    _sessions[session_id] = {
        "_ts": time.time(),
        "transcript": transcript,
    }

    return PreprocessResult(
        session_id=session_id,
        conversation_count=len(conversations),
        raw_message_count=raw_msg_count,
        filtered_message_count=len(kept_msgs),
        estimated_tokens=estimated_tokens,
    )


# ── Context Window Estimation ──────────────────────────────────────────────

def estimate_context_fit(model_id: str, token_count: int) -> dict:
    """Check if estimated tokens fit within the model's context window.

    Returns {fits: bool, context_window: int|None, pct: float|None}.
    """
    model_lower = model_id.lower()
    for key, ctx in MODEL_CONTEXT_WINDOWS.items():
        if key in model_lower:
            pct = token_count / ctx
            return {"fits": pct < 0.8, "context_window": ctx, "pct": round(pct, 2)}
    return {"fits": True, "context_window": None, "pct": None}


# ── LLM Extraction (background thread) ────────────────────────────────────

def start_extract_job(session_id: str, model_name: str,
                      granularity: str = "standard") -> str:
    """Start a background extraction job. Returns job_id for polling."""
    _cleanup_stale(_jobs, _JOB_TTL)

    session = _sessions.get(session_id)
    if not session:
        raise KeyError("Session 不存在或已过期，请重新上传文件")

    job_id = uuid.uuid4().hex[:16]
    _jobs[job_id] = {
        "_ts": time.time(),
        "status": "running",
        "result": None,
        "error": None,
    }

    t = threading.Thread(
        target=_run_extract,
        args=(job_id, session_id, model_name, granularity),
        daemon=True,
    )
    t.start()
    return job_id


def _run_extract(job_id: str, session_id: str, model_name: str,
                 granularity: str = "standard") -> None:
    """Run LLM extraction in a background thread."""
    try:
        transcript = _sessions[session_id]["transcript"]

        # Get model credentials from DB (unmasked)
        from mochi.admin.admin_db import get_model
        entry = get_model(model_name, mask_key=False)
        if not entry:
            raise ValueError(f"模型 '{model_name}' 未找到")

        # Build a one-off LLM client (not through model pool —
        # migration is a one-time operation where the user picks a
        # specific model, not a tier)
        from mochi.llm import _make_client
        client = _make_client(
            provider=entry["provider"],
            api_key=entry["api_key"],
            model=entry["model"],
            base_url=entry.get("base_url", ""),
        )

        messages = [
            {"role": "system", "content": _build_extraction_prompt(granularity)},
            {"role": "user", "content": transcript},
        ]
        response = client.chat(messages, temperature=0.2, max_tokens=4096)
        content = response.content.strip()

        # Parse JSON — handle possible markdown fences
        parsed = _parse_llm_json(content)

        _jobs[job_id]["status"] = "done"
        _jobs[job_id]["result"] = parsed
        log.info("Migration extraction job %s completed", job_id)

    except Exception as e:
        log.exception("Migration extraction job %s failed", job_id)
        _jobs[job_id]["status"] = "error"
        _jobs[job_id]["error"] = str(e)[:1000]


def _parse_llm_json(text: str) -> dict:
    """Parse JSON from LLM response, handling markdown code fences."""
    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try extracting from markdown fence
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # Try finding the outermost {...}
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass

    raise ValueError("LLM 返回的内容无法解析为 JSON，请重试或换用其他模型")


def get_job_status(job_id: str) -> dict:
    """Get the status of an extraction or apply job."""
    job = _jobs.get(job_id)
    if not job:
        raise KeyError("任务不存在或已过期")
    result = {
        "status": job["status"],
        "result": job.get("result"),
        "error": job.get("error"),
    }
    # Include progress for apply jobs
    if "progress" in job:
        result["progress"] = job["progress"]
        result["total"] = job["total"]
    return result


# ── Apply Migration Results (per-section) ─────────────────────────────────

def apply_section(section: str, content: str) -> dict:
    """Write a single section (soul / user_profile / core_memory).

    Synchronous — fast enough for direct call via asyncio.to_thread().
    """
    from mochi.config import OWNER_USER_ID
    from mochi import db
    from mochi.prompt_loader import reload_all

    uid = OWNER_USER_ID or 0
    content = (content or "").strip()
    if not content:
        return {"ok": True, "written": False}

    if section == "soul":
        path = _DATA_PROMPTS_DIR / "system_chat" / "soul.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        reload_all()
    elif section == "user_profile":
        path = _DATA_PROMPTS_DIR / "system_chat" / "user.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        reload_all()
    elif section == "core_memory":
        db.update_core_memory(uid, content)
    else:
        return {"ok": False, "error": f"未知 section: {section}"}

    return {"ok": True, "written": True}


def start_apply_memories_job(items: list[dict]) -> str:
    """Start a background job to import memory items. Returns job_id."""
    _cleanup_stale(_jobs, _JOB_TTL)

    job_id = uuid.uuid4().hex[:16]
    # Count selected items for progress tracking
    selected = [it for it in items if it.get("selected", True) and (it.get("content") or "").strip()]
    _jobs[job_id] = {
        "_ts": time.time(),
        "status": "running",
        "result": None,
        "error": None,
        "progress": 0,
        "total": min(len(selected), _MAX_MEMORY_ITEMS),
    }

    t = threading.Thread(
        target=_run_apply_memories,
        args=(job_id, items),
        daemon=True,
    )
    t.start()
    return job_id


def _run_apply_memories(job_id: str, items: list[dict]) -> None:
    """Import memory items in a background thread with progress tracking."""
    try:
        from mochi.config import OWNER_USER_ID
        from mochi import db
        from mochi.model_pool import get_pool

        uid = OWNER_USER_ID or 0
        pool = get_pool()
        imported = 0

        for item in items:
            if not item.get("selected", True):
                continue
            content = (item.get("content") or "").strip()
            if not content:
                continue
            if imported >= _MAX_MEMORY_ITEMS:
                break
            category = item.get("category", "其他")
            importance = max(1, min(3, int(item.get("importance", 1))))
            embedding = pool.embed(content)
            db.save_memory_item(
                uid,
                category=category,
                content=content,
                importance=importance,
                source="migration",
                embedding=embedding,
            )
            imported += 1
            _jobs[job_id]["progress"] = imported

        _jobs[job_id]["status"] = "done"
        _jobs[job_id]["result"] = {"memory_items_imported": imported}
        log.info("Migration apply job %s completed: %d items", job_id, imported)

    except Exception as e:
        log.exception("Migration apply job %s failed", job_id)
        _jobs[job_id]["status"] = "error"
        _jobs[job_id]["error"] = str(e)[:1000]
