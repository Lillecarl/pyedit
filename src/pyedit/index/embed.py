"""Text to vectors. The index talks to an `Embedder`; `hosted` builds
one from aid-models (`aid_models`), a soft dependency like pyjj."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import anyio

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
    from contextlib import AbstractAsyncContextManager

    Embed = Callable[[Sequence[str]], Awaitable[list[list[float]]]]

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

    def connect(self) -> AbstractAsyncContextManager[Embed]:
        """One connection's embed function, callable concurrently."""
        ...


def embed_once(embedder: Embedder, texts: Sequence[str]) -> list[list[float]]:
    async def run() -> list[list[float]]:
        async with embedder.connect() as embed:
            return await embed(texts)

    return anyio.run(run)


def embed_batches(
    embedder: Embedder,
    batches: Sequence[Sequence[str]],
    done: Callable[[int, list[list[float]]], None],
    concurrency: int,
) -> None:
    """Embed every batch, `concurrency` at once. `done` hears each one's
    place and vectors as it finishes. The first failure cancels the rest
    and is raised as itself, not in a group."""

    failures: list[Exception] = []

    async def run() -> None:
        limiter = anyio.CapacityLimiter(concurrency)

        async def one(place: int, batch: Sequence[str]) -> None:
            async with limiter:
                try:
                    vectors = await embed(batch)
                except Exception as error:
                    failures.append(error)
                    group.cancel_scope.cancel()
                    return
            done(place, vectors)

        async with embedder.connect() as embed, anyio.create_task_group() as group:
            for place, batch in enumerate(batches):
                group.start_soon(one, place, batch)

    anyio.run(run)
    if failures:
        raise failures[0]


@dataclass
class Hosted:
    """An aid-models embedder."""

    model: str = DEFAULT_MODEL
    dimensions: int = DEFAULT_DIMENSIONS
    api_base: str = DEFAULT_API_BASE
    key_env: str = DEFAULT_KEY_ENV
    # 429s and 5xx back off 0.5 s, doubling: about two minutes in all
    max_retries: int = 8
    tokens: int = 0
    cost: float | None = None

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[Embed]:
        from aid_models.embedding import OpenAIEmbedder
        from aid_models.transport import Provider

        client = OpenAIEmbedder(
            self.model,
            Provider(self.api_base, self.key_env),
            dimensions=self.dimensions,
            settings={"max_retries": self.max_retries},
        )

        async def embed(texts: Sequence[str]) -> list[list[float]]:
            got = await client.embed(texts)
            self.tokens += got.tokens
            if got.cost is not None:
                self.cost = (self.cost or 0.0) + got.cost
            return got.vectors

        try:
            yield embed
        finally:
            await client.aclose()


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
