"""Text to vectors. The index talks to an `Embedder`; `hosted` builds
one from aid-models (`aid_models`), a soft dependency like pyjj."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

DEFAULT_MODEL = "openrouter/qwen/qwen3-embedding-8b"
DEFAULT_DIMENSIONS = 1024
DEFAULT_API_BASE = "https://openrouter.ai/api/v1"
DEFAULT_KEY_ENV = "OPENROUTER_API_KEY"
# Qwen3-Embedding wants an instruction on queries and none on documents
QUERY_PREFIX = (
    "Instruct: Given a description of code, retrieve the code it describes\nQuery: "
)


class EmbedderMissing(RuntimeError):
    """pyedit was built without aid-models."""


class Embedder(Protocol):
    model: str
    dimensions: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass
class Hosted:
    """An aid-models embedder, run to completion per call."""

    model: str = DEFAULT_MODEL
    dimensions: int = DEFAULT_DIMENSIONS
    api_base: str = DEFAULT_API_BASE
    key_env: str = DEFAULT_KEY_ENV
    tokens: int = 0
    cost: float | None = None

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        import anyio

        return anyio.run(self._embed, list(texts))

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        from aid_models.embedding import OpenAIEmbedder
        from aid_models.transport import Provider

        client = OpenAIEmbedder(
            self.model,
            Provider(self.api_base, self.key_env),
            dimensions=self.dimensions,
        )
        try:
            got = await client.embed(texts)
        finally:
            await client.aclose()
        self.tokens += got.tokens
        if got.cost is not None:
            self.cost = (self.cost or 0.0) + got.cost
        return got.vectors


def hosted(
    model: str = DEFAULT_MODEL,
    dimensions: int = DEFAULT_DIMENSIONS,
    api_base: str = DEFAULT_API_BASE,
    key_env: str = DEFAULT_KEY_ENV,
) -> Hosted:
    try:
        import aid_models.embedding  # noqa: F401
    except ImportError:
        raise EmbedderMissing(
            "the code index embeds through aid-models, and this pyedit was "
            "built without it; install pyedit with aid-models (nix: pass "
            "`aid-models` to pyedit's default.nix)"
        ) from None
    return Hosted(model, dimensions, api_base, key_env)
