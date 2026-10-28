"""Access to a workspace (config + database), shared by CLI, MCP server and hooks."""

from __future__ import annotations

import contextlib
import time
from pathlib import Path

from sqlalchemy import Connection, Engine

from wingman import embeddings, summaries
from wingman.config import Config, ConfigError, load_config
from wingman.storage import db, store


class WorkspaceError(Exception):
    pass


EMBEDDING_FAILURE_FILE = "embeddings-unavailable"
EMBEDDING_RETRY_SECONDS = 24 * 3600


class Workspace:
    """Lazily loads configuration and database."""

    def __init__(self, home: Path | None = None) -> None:
        self._home = home
        self._config: Config | None = None
        self._engine: Engine | None = None
        self._embedder: embeddings.Embedder | None = None
        self._embedder_checked = False
        self._summarizer: summaries.Summarizer | None = None
        self.warnings: list[str] = []
        """Non-fatal problems (e.g. semantic search unavailable), for the caller to show."""

    @property
    def config(self) -> Config:
        if self._config is None:
            try:
                self._config = load_config(self._home)
            except ConfigError as exc:
                raise WorkspaceError(str(exc)) from exc
        return self._config

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            engine = db.create_db_engine(self.config.db_url)
            if not db.is_up_to_date(engine):
                if not db.is_sqlite(self.config.db_url):
                    raise WorkspaceError(
                        "Database schema is out of date. Run 'wingman db upgrade'."
                    )
                db.upgrade(engine)
            self._engine = engine
        return self._engine

    def workspace_id(self, conn: Connection) -> int:
        try:
            return store.get_workspace_id(conn, self.config.workspace)
        except store.StoreError as exc:
            raise WorkspaceError(str(exc)) from exc

    @property
    def embedder(self) -> embeddings.Embedder | None:
        """The configured embedder; None if semantic search is off or unavailable."""
        if not self._embedder_checked:
            self._embedder_checked = True
            if self.config.embeddings.enabled is None and self._recent_embedding_failure():
                # "auto": do not wait for a failing model download on every command
                self._warn(
                    "semantic search skipped: the embedding model could not be loaded "
                    "recently (run `wingman embed` to retry)"
                )
                return None
            try:
                self._embedder = embeddings.create_embedder(self.config.embeddings)
            except embeddings.EmbeddingError as exc:
                self._warn(str(exc))
        return self._embedder

    def _failure_file(self) -> Path:
        return self.config.home / EMBEDDING_FAILURE_FILE

    def _recent_embedding_failure(self) -> bool:
        try:
            age = time.time() - self._failure_file().stat().st_mtime
        except OSError:
            return False
        return age < EMBEDDING_RETRY_SECONDS

    def embedding_failed(self, error: Exception) -> None:
        """Remember a failed model load (skipped in "auto" mode for a while)."""
        self._embedder = None
        self._warn(f"semantic search disabled: {error}")
        with contextlib.suppress(OSError):
            self._failure_file().write_text(f"{error}\n", encoding="utf-8")

    def retry_embeddings(self) -> None:
        """Forget a previous failure and load the embedder again on next use."""
        self._failure_file().unlink(missing_ok=True)
        self._embedder, self._embedder_checked = None, False

    @property
    def summarizer(self) -> summaries.Summarizer:
        """The LLM client for ``wingman summarize``; raises SummaryError if unusable."""
        if self._summarizer is None:
            self._summarizer = summaries.AnthropicSummarizer(self.config.summaries.model)
        return self._summarizer

    def set_summarizer(self, summarizer: summaries.Summarizer) -> None:
        """Use a specific summarizer (tests, other providers)."""
        self._summarizer = summarizer

    def restore_summaries(self) -> int:
        """Re-attach cached LLM summaries to re-indexed symbols (free, no API call)."""
        if not self.config.summaries.enabled:
            return 0
        with self.engine.connect() as conn:
            workspace_id = self.workspace_id(conn)
        return summaries.apply_cached(self.engine, workspace_id, self.config.summaries)

    def set_embedder(self, embedder: embeddings.Embedder | None) -> None:
        """Use a specific embedder (tests, other providers)."""
        self._embedder, self._embedder_checked = embedder, True

    def semantic_search(self, text: str, limit: int = 200) -> list[tuple[int, float]] | None:
        """Semantic candidates for ``text``, or None if unavailable (reason in warnings)."""
        embedder = self.embedder
        if embedder is None:
            return None
        with self.engine.connect() as conn:
            workspace_id = self.workspace_id(conn)
        try:
            return embeddings.semantic_search(
                self.engine, embedder, workspace_id, text, limit=limit
            )
        except embeddings.EmbeddingError as exc:
            self.embedding_failed(exc)  # do not retry a broken model in this process
            return None

    def _warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def repositories(self) -> list[store.RepoInfo]:
        with self.engine.connect() as conn:
            return store.list_repositories(conn, self.workspace_id(conn))

    def locate(self, path: Path) -> tuple[store.RepoInfo, str] | None:
        """Repository containing ``path`` and the path relative to its root."""
        absolute = path.resolve()
        best: tuple[store.RepoInfo, str] | None = None
        for repo in self.repositories():
            root = Path(repo.root_path)
            if absolute.is_relative_to(root):
                rel = absolute.relative_to(root).as_posix()
                if best is None or len(repo.root_path) > len(best[0].root_path):
                    best = (repo, rel)
        return best
