import hashlib
import re

import numpy as np
import pytest
from sqlalchemy import func, select

from wingman import context, embeddings, query
from wingman.config import EmbeddingConfig
from wingman.storage.schema import embedding_cache, symbols
from wingman.storage.search import identifier_words
from wingman.workspace import Workspace

# German/English words mapped to shared concepts, so that a query without any word in
# common with the code can still be "similar" – like a real embedding model would do.
CONCEPTS = {
    "tax": "tax",
    "vat": "tax",
    "mwst": "tax",
    "steuer": "tax",
    "discount": "discount",
    "rabatt": "discount",
    "invoice": "invoice",
    "rechnung": "invoice",
    "round": "round",
    "runden": "round",
    "money": "money",
    "geld": "money",
}


class FakeEmbedder:
    """Deterministic bag-of-concepts embedder for tests."""

    model = "fake-concepts"

    def __init__(self) -> None:
        self.embedded_texts = 0

    def _vector(self, text: str) -> np.ndarray:
        vector = np.zeros(64, dtype=np.float32)
        for token in re.findall(r"[A-Za-z]+", text):
            for word in {token.lower(), *identifier_words(token)}:
                concept = CONCEPTS.get(word)
                if concept:
                    bucket = int(hashlib.md5(concept.encode()).hexdigest(), 16) % 64
                    vector[bucket] = 1.0  # presence, not frequency
        vector[63] += 0.01  # never all zeros
        return vector / np.linalg.norm(vector)

    def embed_documents(self, texts):
        self.embedded_texts += len(texts)
        return np.vstack([self._vector(t) for t in texts])

    def embed_query(self, text):
        return self._vector(text)


@pytest.fixture
def fake():
    return FakeEmbedder()


def _embed(indexed, fake):
    return embeddings.ensure_embeddings(indexed.engine, fake, indexed.workspace_id)


def test_embed_hash_only_for_embeddable_kinds(indexed):
    with indexed.engine.connect() as conn:
        rows = conn.execute(select(symbols.c.kind, symbols.c.embed_hash)).all()
    assert all(bool(h) == (k in embeddings.EMBED_KINDS) for k, h in rows)
    assert any(k == "property" for k, _ in rows)


def test_embeddings_are_computed_once_and_survive_reindexing(indexed, fake):
    first = _embed(indexed, fake)
    assert first.embedded == first.pending > 30
    assert first.skipped == 0
    assert _embed(indexed, fake).pending == 0

    indexed.reindex("ts_app", full=True)  # new symbol ids, same texts
    indexed.reindex("cs_app", full=True)
    assert _embed(indexed, fake).pending == 0
    assert fake.embedded_texts == first.embedded


def test_only_changed_symbols_are_re_embedded(indexed, fake):
    _embed(indexed, fake)
    money = indexed.root / "ts_app/src/shared/money.ts"
    money.write_text(money.read_text().replace("* 100) / 100", "* 1000) / 1000"))
    indexed.reindex("ts_app")
    stats = _embed(indexed, fake)
    assert stats.embedded == 1  # roundMoney; the const VAT_RATE has no embedding


def test_files_changed_after_indexing_are_skipped(indexed, fake):
    money = indexed.root / "ts_app/src/shared/money.ts"
    money.write_text(money.read_text() + "\n// edited, not re-indexed yet\n")
    stats = _embed(indexed, fake)
    assert stats.skipped == 1
    assert stats.embedded == stats.pending


def test_budget_limits(indexed, fake):
    stats = embeddings.ensure_embeddings(
        indexed.engine, fake, indexed.workspace_id, max_texts=5, batch_size=2
    )
    assert stats.embedded == 5
    with indexed.engine.connect() as conn:
        done, total = embeddings.coverage(conn, indexed.workspace_id, fake.model)
    assert done == 5 < total


def test_semantic_search_finds_code_without_shared_words(indexed, fake):
    query_text = "Rabatt MwSt Rechnung"  # German; no word occurs in the code
    with indexed.engine.connect() as conn:
        assert query.search(conn, indexed.workspace_id, query_text) == []

    similar = embeddings.semantic_search(indexed.engine, fake, indexed.workspace_id, query_text)
    with indexed.engine.connect() as conn:
        hits = query.search(conn, indexed.workspace_id, query_text, semantic=similar, limit=5)
    names = [h.qualified_name for h in hits]
    tax_calculator = {
        "TaxCalculator",
        "TaxCalculator.breakdown",
        "Billing.Tax.TaxCalculator",
        "Billing.Tax.TaxCalculator.Breakdown",
    }
    assert tax_calculator & set(names[:3]), names
    assert all(h.matched_by == "meaning" for h in hits)


def test_hybrid_combines_text_and_meaning(indexed, fake):
    similar = embeddings.semantic_search(indexed.engine, fake, indexed.workspace_id, "round money")
    with indexed.engine.connect() as conn:
        hits = query.search(conn, indexed.workspace_id, "round money", semantic=similar, limit=3)
    assert hits[0].qualified_name == "roundMoney"
    assert hits[0].matched_by == "text+meaning"


def test_vectors_are_cached_until_the_index_changes(indexed, fake):
    _embed(indexed, fake)
    with indexed.engine.connect() as conn:
        ids, matrix = embeddings.load_vectors(conn, indexed.workspace_id, fake.model)
        again, matrix_again = embeddings.load_vectors(conn, indexed.workspace_id, fake.model)
    assert matrix_again is matrix
    (indexed.root / "ts_app/src/ui/InvoiceView.tsx").unlink()
    indexed.reindex("ts_app")
    with indexed.engine.connect() as conn:
        fewer, _ = embeddings.load_vectors(conn, indexed.workspace_id, fake.model)
    assert len(fewer) < len(ids)


def test_context_pack_uses_semantic_candidates(indexed, fake):
    def semantic(text):
        return embeddings.semantic_search(indexed.engine, fake, indexed.workspace_id, text)

    with indexed.engine.connect() as conn:
        pack = context.find_relevant(
            conn, indexed.workspace_id, "Der Rabatt fehlt bei der MwSt", semantic=semantic
        )
    top = pack.entries[0]
    assert "tax" in top.hit.qualified_name.lower()
    assert any("similar meaning" in reason for reason in top.reasons)


def test_workspace_semantic_search_degrades_gracefully(ws, tmp_path, monkeypatch):
    workspace = Workspace(tmp_path / ".wingman")
    assert workspace.semantic_search("anything") is None  # disabled in tests

    monkeypatch.setenv("WINGMAN_EMBEDDINGS", "on")
    monkeypatch.setattr(embeddings, "provider_available", lambda: False)
    workspace = Workspace(tmp_path / ".wingman")
    assert workspace.semantic_search("anything") is None
    assert "no provider is installed" in workspace.warnings[0]

    class Broken(FakeEmbedder):
        def embed_documents(self, texts):
            raise embeddings.EmbeddingError("model download failed")

    workspace = Workspace(tmp_path / ".wingman")
    workspace.set_embedder(Broken())
    assert workspace.semantic_search("tax") is None
    assert "semantic search disabled: model download failed" in workspace.warnings[0]
    assert workspace.embedder is None  # not retried


def test_create_embedder_config():
    assert embeddings.create_embedder(EmbeddingConfig(enabled=False)) is None


def test_fastembed_errors_are_wrapped(monkeypatch):
    fastembed = pytest.importorskip("fastembed")

    def fail(**kwargs):
        raise RuntimeError("no network")

    monkeypatch.setattr(fastembed, "TextEmbedding", fail)
    embedder = embeddings.FastEmbedEmbedder("some/model")
    with pytest.raises(embeddings.EmbeddingError, match="Could not load embedding model"):
        embedder.embed_query("x")


def test_cli_semantic_status_and_embed_command(ws):
    assert "Semantic:  off" in ws("status").output
    result = ws("embed", ok=False)
    assert result.exit_code == 1
    assert "disabled" in result.output


def test_mcp_search_uses_semantic_candidates(ws, tmp_path, fake):
    import anyio
    from mcp.client import Client

    from wingman.mcp_server import create_server

    workspace = Workspace(tmp_path / ".wingman")
    workspace.set_embedder(fake)
    server = create_server(workspace=workspace)

    async def go():
        async with Client(server) as client:
            result = await client.call_tool("search_code", {"query": "Rabatt MwSt", "limit": 3})
            return result.content[0].text

    text = anyio.run(go)
    assert "TaxCalculator" in text
    with workspace.engine.connect() as conn:
        stored = conn.execute(select(func.count()).select_from(embedding_cache)).scalar_one()
    assert stored > 0  # embeddings were computed lazily before the query
