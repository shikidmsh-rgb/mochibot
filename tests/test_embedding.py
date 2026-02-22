"""Tests for pluggable embedding provider resolution and factory."""

import pytest
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# _resolve_embedding_config tests
# ---------------------------------------------------------------------------

class TestResolveEmbeddingConfig:
    """Test the config resolution priority chain."""

    def _resolve(self, **overrides):
        """Call _resolve_embedding_config with monkeypatched config vars."""
        defaults = {
            "EMBEDDING_PROVIDER": "",
            "EMBEDDING_API_KEY": "",
            "EMBEDDING_MODEL": "",
            "EMBEDDING_BASE_URL": "",
            "AZURE_EMBEDDING_ENDPOINT": "",
            "AZURE_EMBEDDING_API_KEY": "",
            "AZURE_EMBEDDING_DEPLOYMENT": "text-embedding-3-small",
        }
        defaults.update(overrides)
        with patch.multiple("mochi.model_pool", **defaults):
            from mochi.model_pool import _resolve_embedding_config
            return _resolve_embedding_config()

    def test_explicit_none(self):
        provider, key, model, base = self._resolve(EMBEDDING_PROVIDER="none")
        assert provider == "none"
        assert key == ""
        assert model == ""

    def test_explicit_openai(self):
        provider, key, model, base = self._resolve(
            EMBEDDING_PROVIDER="openai",
            EMBEDDING_API_KEY="sk-test",
            EMBEDDING_MODEL="text-embedding-3-large",
        )
        assert provider == "openai"
        assert key == "sk-test"
        assert model == "text-embedding-3-large"
        assert base == ""

    def test_openai_default_model(self):
        """When EMBEDDING_MODEL is empty, should default to text-embedding-3-small."""
        provider, key, model, base = self._resolve(
            EMBEDDING_PROVIDER="openai",
            EMBEDDING_API_KEY="sk-test",
        )
        assert model == "text-embedding-3-small"

    def test_explicit_ollama(self):
        provider, key, model, base = self._resolve(
            EMBEDDING_PROVIDER="ollama",
            EMBEDDING_MODEL="nomic-embed-text",
        )
        assert provider == "ollama"
        assert key == "ollama"  # dummy key
        assert model == "nomic-embed-text"
        assert base == "http://localhost:11434/v1"

    def test_ollama_custom_base_url(self):
        provider, key, model, base = self._resolve(
            EMBEDDING_PROVIDER="ollama",
            EMBEDDING_MODEL="nomic-embed-text",
            EMBEDDING_BASE_URL="http://myhost:11434/v1",
        )
        assert base == "http://myhost:11434/v1"

    def test_explicit_azure(self):
        provider, key, model, base = self._resolve(
            EMBEDDING_PROVIDER="azure_openai",
            EMBEDDING_API_KEY="az-key",
            EMBEDDING_MODEL="my-deployment",
            EMBEDDING_BASE_URL="https://myorg.openai.azure.com",
        )
        assert provider == "azure_openai"
        assert key == "az-key"
        assert model == "my-deployment"
        assert base == "https://myorg.openai.azure.com"

    def test_azure_fallback_to_legacy_vars(self):
        """When EMBEDDING_PROVIDER=azure_openai but new vars are empty,
        should fall back to AZURE_EMBEDDING_* vars."""
        provider, key, model, base = self._resolve(
            EMBEDDING_PROVIDER="azure_openai",
            AZURE_EMBEDDING_API_KEY="legacy-key",
            AZURE_EMBEDDING_DEPLOYMENT="legacy-deploy",
            AZURE_EMBEDDING_ENDPOINT="https://legacy.openai.azure.com",
        )
        assert key == "legacy-key"
        assert model == "legacy-deploy"
        assert base == "https://legacy.openai.azure.com"

    def test_legacy_azure_autodetect(self):
        """When EMBEDDING_PROVIDER is empty but legacy Azure vars are set,
        should auto-detect as azure_openai."""
        provider, key, model, base = self._resolve(
            AZURE_EMBEDDING_ENDPOINT="https://legacy.openai.azure.com",
            AZURE_EMBEDDING_API_KEY="legacy-key",
        )
        assert provider == "azure_openai"
        assert key == "legacy-key"
        assert model == "text-embedding-3-small"
        assert base == "https://legacy.openai.azure.com"

    def test_nothing_configured(self):
        """When nothing is set, should return 'none' (disabled)."""
        provider, key, model, base = self._resolve()
        assert provider == "none"

    def test_unknown_provider(self):
        """Unknown provider string should resolve to 'none'."""
        provider, key, model, base = self._resolve(EMBEDDING_PROVIDER="banana")
        assert provider == "none"


# ---------------------------------------------------------------------------
# _make_embed_client tests
# ---------------------------------------------------------------------------

class TestMakeEmbedClient:
    """Test the embedding client factory."""

    def test_none_returns_none(self):
        from mochi.model_pool import _make_embed_client
        client, model = _make_embed_client("none", "", "", "")
        assert client is None
        assert model == ""

    def test_empty_returns_none(self):
        from mochi.model_pool import _make_embed_client
        client, model = _make_embed_client("", "", "", "")
        assert client is None
        assert model == ""

    @patch("mochi.model_pool.OpenAI", create=True)
    def test_openai_instantiation(self, mock_openai_cls):
        """Should create an OpenAI client with correct kwargs."""
        # Patch at the point of import inside the function
        mock_client = MagicMock()
        with patch.dict("sys.modules", {}):
            with patch("openai.OpenAI", return_value=mock_client) as mock_cls:
                from mochi.model_pool import _make_embed_client
                client, model = _make_embed_client(
                    "openai", "sk-test", "text-embedding-3-small", "",
                )
                mock_cls.assert_called_once_with(api_key="sk-test")
                assert client is mock_client
                assert model == "text-embedding-3-small"

    @patch("openai.AzureOpenAI")
    def test_azure_instantiation(self, mock_azure_cls):
        """Should create an AzureOpenAI client with correct kwargs."""
        mock_client = MagicMock()
        mock_azure_cls.return_value = mock_client
        from mochi.model_pool import _make_embed_client
        client, model = _make_embed_client(
            "azure_openai", "az-key", "my-deploy",
            "https://myorg.openai.azure.com",
        )
        mock_azure_cls.assert_called_once()
        call_kwargs = mock_azure_cls.call_args
        assert call_kwargs.kwargs["azure_endpoint"] == "https://myorg.openai.azure.com"
        assert call_kwargs.kwargs["api_key"] == "az-key"
        assert client is mock_client
        assert model == "my-deploy"

    @patch("openai.OpenAI")
    def test_ollama_uses_openai_client(self, mock_openai_cls):
        """Ollama should use the standard OpenAI client with base_url."""
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        from mochi.model_pool import _make_embed_client
        client, model = _make_embed_client(
            "ollama", "ollama", "nomic-embed-text",
            "http://localhost:11434/v1",
        )
        mock_openai_cls.assert_called_once_with(
            api_key="ollama",
            base_url="http://localhost:11434/v1",
        )
        assert client is mock_client
        assert model == "nomic-embed-text"
