"""Multi-model pool — tier-based routing for all LLM tasks.

Three tiers:
    lite  — cheap/fast model for classification, tagging, simple tasks
    chat  — balanced model for daily conversation (default)
    deep  — strong model for background reasoning, memory ops, analysis

All tier config comes from DB. .env model vars are seed data only —
auto-imported on first startup via seed_models_from_env().
"""

import struct
import logging
import time
import threading
from collections import OrderedDict

from mochi.config import (
    EMBEDDING_PROVIDER, EMBEDDING_API_KEY, EMBEDDING_MODEL, EMBEDDING_BASE_URL,
    AZURE_EMBEDDING_ENDPOINT, AZURE_EMBEDDING_API_KEY,
    AZURE_EMBEDDING_DEPLOYMENT, AZURE_EMBEDDING_API_VERSION,
    EMBEDDING_CACHE_MAX_SIZE, EMBEDDING_CACHE_TTL_S,
)
from mochi.llm import LLMProvider, _make_client

log = logging.getLogger(__name__)

VALID_TIERS = frozenset({"lite", "chat", "deep"})


# ---------------------------------------------------------------------------
# TTL LRU cache (thread-safe, per-entry expiry)
# ---------------------------------------------------------------------------

class _TTLCache:
    """Thread-safe LRU cache with per-entry TTL expiry."""

    def __init__(self, max_size: int = 128, ttl_s: int = 300):
        self._max_size = max_size
        self._ttl_s = ttl_s
        self._data: OrderedDict[str, tuple[float, object]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> object | None:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            ts, val = entry
            if time.monotonic() - ts > self._ttl_s:
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return val

    def put(self, key: str, value: object) -> None:
        with self._lock:
            self._data[key] = (time.monotonic(), value)
            self._data.move_to_end(key)
            while len(self._data) > self._max_size:
                self._data.popitem(last=False)


# ---------------------------------------------------------------------------
# Embedding provider resolution + factory
# ---------------------------------------------------------------------------

def _resolve_embedding_config() -> tuple[str, str, str, str]:
    """Resolve (provider, api_key, model, base_url) for embedding.

    Priority:
      1. EMBEDDING_PROVIDER set → use new EMBEDDING_* vars
      2. Legacy AZURE_EMBEDDING_* vars present → auto-detect as azure_openai
      3. Nothing configured → "none" (disabled)
    """
    provider = (EMBEDDING_PROVIDER or "").strip().lower()

    if provider == "none":
        return ("none", "", "", "")

    if provider == "openai":
        return (
            "openai",
            EMBEDDING_API_KEY,
            EMBEDDING_MODEL or "text-embedding-3-small",
            EMBEDDING_BASE_URL,
        )

    if provider == "azure_openai":
        return (
            "azure_openai",
            EMBEDDING_API_KEY or AZURE_EMBEDDING_API_KEY,
            EMBEDDING_MODEL or AZURE_EMBEDDING_DEPLOYMENT,
            EMBEDDING_BASE_URL or AZURE_EMBEDDING_ENDPOINT,
        )

    if provider == "ollama":
        return (
            "ollama",
            "ollama",  # dummy key required by SDK
            EMBEDDING_MODEL or "nomic-embed-text",
            EMBEDDING_BASE_URL or "http://localhost:11434/v1",
        )

    if provider:
        log.warning("Unknown EMBEDDING_PROVIDER '%s', disabling embedding", provider)
        return ("none", "", "", "")

    # Auto-detect from legacy Azure vars
    if AZURE_EMBEDDING_ENDPOINT and AZURE_EMBEDDING_API_KEY:
        return (
            "azure_openai",
            AZURE_EMBEDDING_API_KEY,
            AZURE_EMBEDDING_DEPLOYMENT,
            AZURE_EMBEDDING_ENDPOINT,
        )

    return ("none", "", "", "")


def _make_embed_client(provider: str, api_key: str, model: str,
                       base_url: str) -> tuple:
    """Instantiate an OpenAI-compatible embedding client, or (None, "").

    Pure factory — provider-specific defaults belong in _resolve_embedding_config.
    """
    if provider == "none" or not provider:
        return None, ""

    if provider == "azure_openai":
        from openai import AzureOpenAI
        client = AzureOpenAI(
            azure_endpoint=base_url,
            api_key=api_key,
            api_version=AZURE_EMBEDDING_API_VERSION,
        )
        return client, model

    # openai + ollama both use the standard OpenAI client
    from openai import OpenAI
    kwargs: dict = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    return OpenAI(**kwargs), model


# ---------------------------------------------------------------------------
# ModelPool
# ---------------------------------------------------------------------------

class ModelPool:
    """Manages LLM clients for all three tiers plus embedding."""

    def __init__(self):
        self._tiers: dict[str, LLMProvider] = {}
        self._tier_models: dict[str, str] = {}
        self._lock = threading.Lock()

        # Load all tiers from DB (the single authority)
        self._load_from_db()

        log.info("Tier pool: %s", {t: m for t, m in self._tier_models.items()})

        # Embedding client (provider-agnostic via _make_embed_client)
        self._embed_client = None
        self._embed_model = ""
        self._embed_cache = _TTLCache(EMBEDDING_CACHE_MAX_SIZE, EMBEDDING_CACHE_TTL_S)

        try:
            e_prov, e_key, e_model, e_base = _resolve_embedding_config()
            self._embed_client, self._embed_model = _make_embed_client(
                e_prov, e_key, e_model, e_base,
            )
            if self._embed_client:
                log.info("Embedding configured: provider=%s model=%s", e_prov, e_model)
            else:
                log.info("Embedding disabled (provider=%s)", e_prov or "none")
        except Exception as e:
            log.warning("Embedding client init failed: %s", e)

    def get_tier(self, tier: str = "chat") -> LLMProvider:
        """Get LLMProvider for a tier. Falls back to 'chat' for unknown tiers."""
        if tier not in self._tiers:
            # Tier missing — maybe models were configured after pool init.
            # Retry DB load once before giving up.
            self._load_from_db()
            if tier not in self._tiers:
                log.warning("Unknown tier '%s', falling back to 'chat'", tier)
                tier = "chat"
        if tier not in self._tiers:
            raise ValueError(
                "No model configured for any tier. "
                "Add a model via the admin portal or set CHAT_MODEL in .env."
            )
        return self._tiers[tier]

    def get_tier_model(self, tier: str) -> str:
        """Get model name for a tier (for logging/admin display)."""
        return self._tier_models.get(tier, self._tier_models.get("chat", "unknown"))

    def reload_tier(self, tier: str, provider: str, api_key: str,
                    model: str, base_url: str) -> None:
        """Hot-swap a tier's LLM client at runtime.

        Called by admin portal after model registry/tier assignment changes.
        Thread-safe via lock.
        """
        if tier not in VALID_TIERS:
            raise ValueError(f"Invalid tier: {tier}")
        client = _make_client(provider, api_key, model, base_url)
        with self._lock:
            self._tiers[tier] = client
            self._tier_models[tier] = model
        log.info("Hot-reloaded tier '%s': provider=%s model=%s", tier, provider, model)

    def _load_from_db(self) -> None:
        """Load all tier configs exclusively from DB."""
        try:
            from mochi.admin.admin_db import get_tier_effective_config
            effective = get_tier_effective_config()
            for tier, cfg in effective.items():
                if not cfg.get("model") or cfg.get("source") == "none":
                    if tier not in self._tiers:
                        log.warning("Tier '%s' has no model assigned", tier)
                    continue
                try:
                    self.reload_tier(
                        tier, cfg["provider"], cfg.get("api_key", ""),
                        cfg["model"], cfg.get("base_url", ""),
                    )
                except Exception as e:
                    log.error("Failed to load tier '%s' from DB: %s", tier, e)
        except Exception as e:
            log.error("Failed to load tier config from DB: %s", e)

    # -------------------------------------------------------------------
    # Embedding
    # -------------------------------------------------------------------

    def embed(self, text: str) -> bytes | None:
        """Generate embedding vector, return as packed float32 bytes. Cached."""
        if not self._embed_client or not text or not text.strip():
            return None
        key = text[:8000]
        cached = self._embed_cache.get(key)
        if cached is not None:
            return cached
        try:
            resp = self._embed_client.embeddings.create(
                model=self._embed_model, input=key,
            )
            vec = resp.data[0].embedding
            packed = struct.pack(f"{len(vec)}f", *vec)
            self._embed_cache.put(key, packed)
            return packed
        except Exception as e:
            log.warning("Embedding failed: %s", e)
            return None

    def embed_batch(self, texts: list[str]) -> list[bytes | None]:
        """Batch-embed multiple texts."""
        if not self._embed_client or not texts:
            return [None] * len(texts)
        try:
            truncated = [t[:8000] for t in texts]
            resp = self._embed_client.embeddings.create(
                model=self._embed_model, input=truncated,
            )
            results: list[bytes | None] = [None] * len(texts)
            for item in resp.data:
                results[item.index] = struct.pack(f"{len(item.embedding)}f", *item.embedding)
            return results
        except Exception as e:
            log.warning("Batch embedding failed: %s", e)
            return [None] * len(texts)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_pool: ModelPool | None = None


def get_pool() -> ModelPool:
    """Get (or create) the global ModelPool singleton."""
    global _pool
    if _pool is None:
        _pool = ModelPool()
    return _pool
