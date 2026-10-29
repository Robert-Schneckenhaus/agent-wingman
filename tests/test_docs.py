import json
import shutil
from pathlib import Path

import pytest
from sqlalchemy import func, select

from wingman import context, docs, embeddings, notes
from wingman.storage.schema import doc_chunks, doc_links, documents

DOCS = Path(__file__).parent / "fixtures" / "docs"


def minimal_pdf(pages: list[str], title: str = "Tax Spec") -> bytes:
    """A small valid PDF with one text line per page (Helvetica)."""
    objects = ["<< /Type /Catalog /Pages 2 0 R >>"]
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(len(pages)))
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>")
    font = 3 + 2 * len(pages)
    for i, text in enumerate(pages):
        content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font} 0 R >> >> /Contents {4 + 2 * i} 0 R >>"
        )
        objects.append(f"<< /Length {len(content)} >>\nstream\n{content.decode()}\nendstream")
    objects.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    objects.append(f"<< /Title ({title}) >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info {len(objects)} 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


def test_markdown_chunks_follow_headings_and_ignore_code_fences():
    title, chunks = docs.chunk_markdown((DOCS / "billing-guide.md").read_text())
    assert title == "Billing Guide"
    assert [(c.heading_path, c.locator) for c in chunks] == [
        ("Billing Guide", "#billing-guide"),
        ("Billing Guide > Tax calculation", "#tax-calculation"),
        ("Billing Guide > Tax calculation > Reduced rates", "#reduced-rates"),
        ("Billing Guide > Discounts", "#discounts"),
    ]
    assert "# not a heading" in chunks[1].text


def test_long_sections_are_split_at_paragraphs():
    text = "# Big\n\n" + "\n\n".join(f"Paragraph {i} " + "word " * 100 for i in range(20))
    _, chunks = docs.chunk_markdown(text)
    assert len(chunks) > 1
    assert all(len(c.text) <= docs.MAX_CHUNK_CHARS for c in chunks)
    assert all(c.heading_path == "Big" for c in chunks)


def test_rst_headings():
    chunks = docs.chunk_text("Intro\n=====\n\nHello.\n\nUsage\n-----\n\nRun it.\n")
    assert [(c.heading_path, c.text) for c in chunks] == [
        ("Intro", "Hello."),
        ("Intro > Usage", "Run it."),
    ]


def test_html_uses_main_content_only():
    title, markdown = docs.html_to_markdown((DOCS / "handbook.html").read_text())
    assert title == "Support Handbook"
    assert "## Refunds" in markdown and "- Full refund within 14 days" in markdown
    assert "noise" not in markdown and "tracking" not in markdown


def test_openapi_operations_become_chunks():
    loaded = docs.load(str(DOCS / "invoices-api.yaml"))
    assert loaded.source_type == "openapi" and loaded.title == "Invoices API"
    locators = [c.locator for c in loaded.chunks]
    assert locators == [None, "GET /api/invoices/{id}/tax", "POST /api/invoices"]
    tax = loaded.chunks[1]
    assert tax.heading_path == "Invoices > getInvoiceTax"
    assert "Tax breakdown of an invoice" in tax.text and "404 Unknown invoice" in tax.text


def test_pdf_pages(tmp_path):
    pytest.importorskip("pypdf")
    path = tmp_path / "spec.pdf"
    path.write_bytes(minimal_pdf(["VAT rules for invoices", "Discounts before tax"]))
    loaded = docs.load(str(path))
    assert loaded.source_type == "pdf" and loaded.title == "Tax Spec"
    assert [(c.locator, c.text) for c in loaded.chunks] == [
        ("page 1", "VAT rules for invoices"),
        ("page 2", "Discounts before tax"),
    ]


def test_url_loading_uses_content_type():
    def fetch(url):
        assert url == "https://wiki.example.com/handbook"
        return (DOCS / "handbook.html").read_bytes(), "text/html; charset=utf-8"

    loaded = docs.load("https://wiki.example.com/handbook", fetch=fetch)
    assert loaded.source_type == "html" and loaded.uri == "https://wiki.example.com/handbook"
    assert any(c.heading_path == "Support Handbook > Refunds" for c in loaded.chunks)


def test_directories_skip_files_that_are_not_documents():
    found = [Path(p).name for p in docs.expand_sources([str(DOCS)])]
    assert found == ["billing-guide.md", "invoices-api.yaml"]  # no package.json, no HTML


def _add(indexed, *sources, **kwargs):
    return docs.add_sources(indexed.engine, indexed.workspace_id, list(sources), **kwargs)


def _links(indexed, document_id):
    with indexed.engine.connect() as conn:
        return {
            (r.heading_path, r.target_key, r.link_source)
            for r in conn.execute(
                select(doc_chunks.c.heading_path, doc_links.c.target_key, doc_links.c.link_source)
                .join(doc_chunks, doc_chunks.c.id == doc_links.c.chunk_id)
                .where(doc_chunks.c.document_id == document_id)
            )
        }


def test_names_and_paths_in_text_are_linked(indexed):
    (result,) = _add(indexed, str(DOCS / "billing-guide.md"))
    assert result.error is None and result.chunks == 4
    links = _links(indexed, result.document_id)
    tax = "Billing Guide > Tax calculation"
    assert (
        tax,
        "cs_app:Billing/TaxCalculator.cs#Billing.Tax.TaxCalculator.Breakdown:method",
        "name_match",
    ) in links
    assert (tax, "ts_app:src/shared/money.ts#roundMoney:function", "name_match") in links
    assert (tax, "ts_app:src/billing/tax.ts", "name_match") in links
    assert not any(h == "Billing Guide > Discounts" for h, _, _ in links)


def test_openapi_operations_link_to_server_endpoints(indexed):
    (result,) = _add(indexed, str(DOCS / "invoices-api.yaml"))
    routes = {(h, k) for h, k, source in _links(indexed, result.document_id) if source == "route"}
    assert routes == {
        (
            "Invoices > getInvoiceTax",
            "cs_app:Api/InvoicesController.cs#Billing.Api.InvoicesController.GetTax:method",
        ),
        (
            "Invoices > Create an invoice",
            "cs_app:Api/InvoicesController.cs#Billing.Api.InvoicesController.Create:method",
        ),
    }


def test_manual_links_and_docs_for_symbols(indexed):
    (result,) = _add(indexed, str(DOCS / "handbook.html"), links=["cs_app:Billing"])
    assert result.links.manual == result.chunks
    with indexed.engine.connect() as conn:
        lookup = context.query.resolve_name(conn, indexed.workspace_id, "TaxCalculator.Breakdown")
        symbol_id = lookup.found[0].id
        linked = docs.docs_for_symbols(conn, indexed.workspace_id, [symbol_id])
    assert [h.title for h in linked[symbol_id]] == ["Support Handbook"]

    with pytest.raises(notes.TargetError, match="Unknown target"), indexed.engine.begin() as conn:
        docs.store_document(
            conn, indexed.workspace_id, docs.load(str(DOCS / "handbook.html")), links=["nope:x"]
        )


def test_search_and_context_pack(indexed):
    _add(indexed, str(DOCS), str(DOCS / "handbook.html"))
    with indexed.engine.connect() as conn:
        hits = docs.search_docs(conn, indexed.workspace_id, "refund credit note")
        assert hits[0].citation == "Support Handbook > Refunds (#refunds)"
        pack = context.find_relevant(
            conn, indexed.workspace_id, "Tax breakdown shows the wrong VAT rate", limit=5
        )
    assert any(d.title == "Billing Guide" for d in pack.docs)
    rendered = context.render_pack(pack)
    assert "## Documentation" in rendered and "billing-guide.md" in rendered
    assert json.dumps(pack.to_dict())  # serializable


def test_readding_unchanged_documents_keeps_chunks_and_refresh_updates(indexed, tmp_path):
    guide = tmp_path / "guide.md"
    shutil.copy(DOCS / "billing-guide.md", guide)
    (first,) = _add(indexed, str(guide))
    (again,) = _add(indexed, str(guide))
    assert again.document_id == first.document_id and not again.changed

    guide.write_text(
        guide.read_text() + "\n## Credit notes\n\nUse `InvoiceService` for credit notes.\n"
    )
    (refreshed,) = docs.refresh(indexed.engine, indexed.workspace_id)
    assert refreshed.changed and refreshed.chunks == 5
    with indexed.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(documents)).scalar_one() == 1
        hits = docs.search_docs(conn, indexed.workspace_id, "credit notes")
        assert hits[0].heading_path == "Billing Guide > Credit notes"
        assert docs.remove_document(conn, indexed.workspace_id, first.document_id)
        assert docs.search_docs(conn, indexed.workspace_id, "credit notes") == []


def test_errors_are_reported_per_source(indexed, tmp_path):
    empty = tmp_path / "empty.md"
    empty.write_text("   ")
    results = _add(indexed, str(empty), str(tmp_path / "missing.md"), str(DOCS / "handbook.html"))
    assert [bool(r.error) for r in results] == [True, True, False]


def test_chunks_are_embedded_and_searchable_by_meaning(indexed):
    from test_embeddings import FakeEmbedder

    fake = FakeEmbedder()
    _add(indexed, str(DOCS / "billing-guide.md"))
    stats = embeddings.ensure_embeddings(indexed.engine, fake, indexed.workspace_id)
    assert stats.skipped == 0
    similar = embeddings.semantic_search(
        indexed.engine, fake, indexed.workspace_id, "Rabatt", owner="chunk"
    )
    with indexed.engine.connect() as conn:
        hits = docs.search_docs(conn, indexed.workspace_id, "Rabatt", semantic=similar, limit=1)
    assert hits[0].heading_path == "Billing Guide > Discounts" and hits[0].reason == "meaning"


def test_cli_doc_commands(ws):
    output = ws("doc", "add", str(DOCS), str(DOCS / "handbook.html"), "--link", "cs_app:Api").output
    assert "Billing Guide (4 chunks, updated)" in output
    assert "Invoices API (3 chunks, updated): " in output
    listing = ws("doc", "list").output
    assert "openapi" in listing and "links: cs_app:Api" in listing
    found = ws("doc", "search", "refunds").output
    assert "Support Handbook > Refunds" in found
    chunk_id = found.split("[chunk ", 1)[1].split("]", 1)[0]
    shown = ws("doc", "show", chunk_id).output
    assert "Refunds are issued as credit notes." in shown and "cs_app:Api" in shown
    assert "docs: Invoices API > Invoices > getInvoiceTax" in ws("symbol", "GetTax").output
    assert "unchanged" in ws("doc", "refresh").output
    ws("doc", "remove", "handbook.html")
    assert "Support Handbook" not in ws("doc", "list").output
    assert ws("doc", "remove", "nope", ok=False).exit_code != 0


def test_docs_inside_a_repository_prefer_its_code(indexed):
    notes_md = indexed.root / "ts_app" / "docs" / "notes.md"
    notes_md.parent.mkdir()
    notes_md.write_text("# Notes\n\nThe `TaxCalculator` and `ITaxCalculator` live in tax.ts.\n")
    (result,) = _add(indexed, str(notes_md))
    keys = {k for _, k, _ in _links(indexed, result.document_id)}
    assert keys and all(k.startswith("ts_app:") for k in keys)
    with indexed.engine.connect() as conn:
        repo_id = conn.execute(
            select(documents.c.repo_id).where(documents.c.id == result.document_id)
        ).scalar_one()
    assert repo_id == indexed.repo("ts_app").id
