"""HTTP/WS models for audio embedder endpoints — Pydantic request/response types."""

from __future__ import annotations

from pydantic import BaseModel, Field

from core.models.audio import RawAudioEmbedding


class EmbedAudioRequest(BaseModel):
    """Request for stateless embedding computation."""

    audios_b64: list[str] = Field(min_length=1, max_length=16)
    return_chunks: bool = False


class EmbedAudioResponse(BaseModel):
    embedding: list[float]
    embedding_dim: int
    chunk_embeddings: list[list[float]] | None = None

    @staticmethod
    def from_raw_embedding(
        raw: RawAudioEmbedding, return_chunks: bool = False
    ) -> "EmbedAudioResponse":
        chunk_payload: list[list[float]] | None = None
        if return_chunks:
            chunk_payload = [row.tolist() for row in raw.chunk_embeddings]
        return EmbedAudioResponse(
            embedding=raw.embedding.tolist(),
            embedding_dim=int(raw.embedding.shape[0]),
            chunk_embeddings=chunk_payload,
        )
