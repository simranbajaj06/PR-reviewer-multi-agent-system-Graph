"""Ports for getting repo code to the graph builder.
(Replaces the RepoSource/Snapshot part of the old rag/ports.py; the chunk,
embedding and reranker ports are gone together with the RAG layer.)
"""
from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Snapshot:
    repo: str
    ref: str
    commit_sha: str
    path: Path


class RepoSource(ABC):
    """Where repo code comes from (GitHub today)."""

    @abstractmethod
    def resolve_ref(self, repo: str, ref: str | None) -> str:
        """Branch/tag/sha (None = default branch) -> exact commit sha."""

    @abstractmethod
    def snapshot(self, repo: str, sha: str) -> AbstractContextManager[Snapshot]:
        """Full checkout in a temp dir, deleted on exit."""