import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from wingman import query, summaries
from wingman.config import SummaryConfig
from wingman.storage.schema import refs, summary_cache, symbols

CONFIG = SummaryConfig(enabled=True, model="fake-llm", min_lines=1)
BLOCK = re.compile(r'<symbol id="(\d+)">\nkind: \w+\nname: (\S+)')


class FakeSummarizer:
    """Answers every symbol with a summary naming it and a unique keyword."""

    model = "fake-llm"

    def __init__(self, fail: Exception | None = None) -> None:
        self.prompts: list[str] = []
        self.fail = fail

    def summarize(self, prompt: str):
        self.prompts.append(prompt)
        if self.fail:
            raise self.fail
        answers = [
            {
                "id": number,
                "summary": f"Summary of {name}.",
                "keywords": [f"kw{name.rsplit('.', 1)[-1].lower()}"],
            }
            for number, name in BLOCK.findall(prompt)
        ]
        return {"summaries": answers}, 1000


def _run(indexed, summarizer, **kwargs):
    return summaries.summarize_workspace(
        indexed.engine, indexed.workspace_id, CONFIG, summarizer, workers=1, **kwargs
    )


def _generated(indexed) -> dict[str, str]:
    with indexed.engine.connect() as conn:
        rows = conn.execute(
            select(symbols.c.qualified_name, symbols.c.summary).where(
                symbols.c.summary_source == "generated"
            )
        )
        return {r.qualified_name: r.summary for r in rows}


def test_summarizes_undocumented_symbols_only(indexed):
    fake = FakeSummarizer()
    stats = _run(indexed, fake)
    assert stats.summarized > 0 and stats.failed == 0 and stats.remaining == 0
    generated = _generated(indexed)
    assert len(generated) == stats.summarized
    with indexed.engine.connect() as conn:
        documented = conn.execute(
            select(func.count()).where(
                symbols.c.summary_source == "generated", symbols.c.doc.is_not(None)
            )
        ).scalar_one()
        cached = conn.execute(select(func.count()).select_from(summary_cache)).scalar_one()
    assert documented == 0
    assert cached > 0
    prompt_text = "\n".join(fake.prompts)
    assert ".test.ts" not in prompt_text and "Billing.Tests" not in prompt_text
    assert all(len(BLOCK.findall(p)) <= summaries.MAX_BATCH_SYMBOLS for p in fake.prompts)

    again = FakeSummarizer()
    assert _run(indexed, again).candidates == 0
    assert again.prompts == []


def test_summaries_are_searchable(indexed):
    _run(indexed, FakeSummarizer())
    name, _ = next(iter(_generated(indexed).items()))
    keyword = f"kw{name.rsplit('.', 1)[-1].lower()}"
    with indexed.engine.connect() as conn:
        hits = query.search(conn, indexed.workspace_id, keyword, limit=5)
        details = query.symbol_details(conn, hits[0].id)
    assert hits[0].qualified_name == name
    assert details.generated_summary == f"Summary of {name}."


def test_callees_are_summarized_before_callers_and_given_as_context(indexed):
    fake = FakeSummarizer()
    _run(indexed, fake)
    generated = _generated(indexed)
    with indexed.engine.connect() as conn:
        caller, callee = (
            select(symbols.c.qualified_name).where(symbols.c.id == column).scalar_subquery()
            for column in (refs.c.from_symbol_id, refs.c.to_symbol_id)
        )
        pairs = [
            (r[0], r[1])
            for r in conn.execute(
                select(caller, callee).where(
                    refs.c.kind == "call", refs.c.to_symbol_id.is_not(None)
                )
            )
            if r[0] in generated and r[1] in generated and r[0] != r[1]
        ]
    assert pairs, "fixtures should contain an undocumented caller of undocumented code"

    def prompt_index(name):
        return next(i for i, p in enumerate(fake.prompts) if f"name: {name}\n" in p)

    for caller_name, callee_name in pairs:
        assert prompt_index(callee_name) <= prompt_index(caller_name)
    with_context = [
        (caller_name, callee_name)
        for caller_name, callee_name in pairs
        if f"- {callee_name}: Summary of {callee_name}." in fake.prompts[prompt_index(caller_name)]
    ]
    assert with_context, pairs


def test_reindexed_symbols_get_their_summary_back_from_the_cache(indexed):
    _run(indexed, FakeSummarizer())
    before = _generated(indexed)
    indexed.reindex("ts_app", full=True)
    assert len(_generated(indexed)) < len(before)

    restored = summaries.apply_cached(indexed.engine, indexed.workspace_id, CONFIG)
    assert restored > 0
    assert _generated(indexed) == before
    fake = FakeSummarizer()
    stats = _run(indexed, fake)
    assert fake.prompts == [] and stats.candidates == 0


def test_summary_changes_the_embedding_text(indexed):
    with indexed.engine.connect() as conn:
        before = dict(conn.execute(select(symbols.c.id, symbols.c.embed_hash)).all())
    _run(indexed, FakeSummarizer())
    with indexed.engine.connect() as conn:
        rows = conn.execute(
            select(symbols.c.id, symbols.c.embed_hash, symbols.c.kind).where(
                symbols.c.summary_source == "generated"
            )
        ).all()
    changed = [r for r in rows if r.embed_hash != before[r.id]]
    assert changed and all(r.embed_hash for r in changed)


def test_budget_stops_the_run(indexed):
    stats = _run(indexed, FakeSummarizer(), max_tokens=1)
    assert stats.budget_exhausted
    assert stats.requests == 1
    assert stats.remaining > 0


def test_limit_and_dry_run(indexed):
    dry = _run(indexed, None, dry_run=True)
    assert dry.candidates > 3 and dry.tokens > 0 and dry.requests > 0
    assert _generated(indexed) == {}

    limited = _run(indexed, FakeSummarizer(), limit=3)
    assert limited.summarized == 3


def test_fatal_error_stops_and_other_errors_skip(indexed):
    fatal = FakeSummarizer(summaries.SummaryError("no key", fatal=True))
    stats = _run(indexed, fatal)
    assert len(fatal.prompts) == 1 and stats.summarized == 0 and stats.errors == ["no key"]

    flaky = FakeSummarizer(summaries.SummaryError("overloaded"))
    stats = _run(indexed, flaky)
    assert len(flaky.prompts) == stats.requests > 1
    assert stats.failed == stats.candidates


def test_levels_handle_cycles():
    deps = {1: {2}, 2: {3}, 3: {2}, 4: set()}
    levels = summaries._levels([1, 2, 3, 4], deps)
    assert levels[4] == 0
    assert levels[1] > max(levels[2], levels[3])


def test_anthropic_summarizer_parses_and_checks_stop_reason():
    pytest.importorskip("anthropic")
    summarizer = summaries.AnthropicSummarizer("claude-haiku-5-5")
    calls = []

    def response(stop_reason, text):
        return SimpleNamespace(
            stop_reason=stop_reason,
            usage=SimpleNamespace(input_tokens=100, output_tokens=20),
            content=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text=text)],
        )

    answers = [response("end_turn", '{"summaries": []}'), response("refusal", "")]

    def create(**kwargs):
        calls.append(kwargs)
        return answers.pop(0)

    summarizer._client = SimpleNamespace(messages=SimpleNamespace(create=create))
    assert summarizer.summarize("x") == ({"summaries": []}, 120)
    assert calls[0]["output_config"]["format"]["schema"] == summaries.OUTPUT_SCHEMA
    assert calls[0]["model"] == "claude-haiku-5-5"
    with pytest.raises(summaries.SummaryError) as error:
        summarizer.summarize("x")
    assert error.value.tokens == 120 and not error.value.fatal


def test_cli_summarize(ws, monkeypatch):
    result = ws("summarize", ok=False)
    assert result.exit_code != 0 and "enabled = true" in result.output
    assert "symbols without documentation" in ws("summarize", "--dry-run").output

    config = Path(".wingman/config.toml")
    text = config.read_text(encoding="utf-8")
    config.write_text(text.replace("# enabled = false", "enabled = true"), encoding="utf-8")
    monkeypatch.setattr(summaries, "AnthropicSummarizer", lambda model: FakeSummarizer())
    output = ws("summarize", "ts_app").output
    assert re.search(r"summaries \(fake-llm\): [1-9]\d* written", output), output
    shown = ws("symbol", "InvoiceView").output
    assert "summary (generated): Summary of InvoiceView." in shown
