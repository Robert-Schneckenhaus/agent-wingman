import anyio
import pytest
from mcp.client import Client

from wingman.mcp_server import create_server
from wingman.workspace import Workspace


@pytest.fixture
def call(ws, tmp_path):
    """Call a tool of an in-memory MCP server for the CLI workspace; returns its text."""
    server = create_server(workspace=Workspace(tmp_path / ".wingman"))

    def run(tool: str, **arguments) -> str:
        async def go():
            async with Client(server) as client:
                result = await client.call_tool(tool, arguments)
                assert not result.is_error, result
                return "\n".join(c.text for c in result.content if getattr(c, "text", None))

        return anyio.run(go)

    return run


def test_lists_tools_with_annotations(ws, tmp_path):
    server = create_server(workspace=Workspace(tmp_path / ".wingman"))

    async def go():
        async with Client(server) as client:
            assert "find_relevant_for_task" in (client.instructions or "")
            return (await client.list_tools()).tools

    tools = {t.name: t for t in anyio.run(go)}
    assert set(tools) == {
        "find_relevant_for_task",
        "search_code",
        "get_symbol",
        "get_callers",
        "get_callees",
        "find_tests_for",
        "get_file_outline",
        "add_note",
        "reindex",
    }
    assert tools["get_symbol"].annotations.read_only_hint is True
    assert tools["add_note"].annotations.read_only_hint is False


def test_find_relevant_for_task(call):
    text = call("find_relevant_for_task", task="VAT line missing in the PDF export", limit=3)
    assert text.startswith("## Relevant code (3 of")
    assert "ts_app:src/export/pdfRenderer.ts:21-23  method PdfRenderer.renderTaxLines" in text


def test_search_and_symbol(call):
    assert "Billing.Models.Invoice.NetTotal" in call("search_code", query="net total", limit=3)
    text = call("get_symbol", name="roundMoney", include_code=True)
    assert "doc: Rounds a monetary amount to two decimals." in text
    assert "return Math.round(value * 100) / 100;" in text


def test_ambiguous_and_unknown_names_return_guidance(call):
    assert "ambiguous" in call("get_symbol", name="Breakdown")
    assert "Try search_code" in call("get_callers", name="NoSuchThing")


def test_navigation_tools(call):
    callers = call("get_callers", name="Billing.Data.BillingContext.SaveEntitiesAsync")
    assert "via Billing.Data.IUnitOfWork.SaveEntitiesAsync" in callers
    callees = call("get_callees", name="PdfRenderer.render")
    assert "this.renderTaxLines -> PdfRenderer.renderTaxLines" in callees
    tests = call("find_tests_for", name="roundMoney")
    assert "test TaxCalculator.applies VAT" in tests
    outline = call("get_file_outline", path="ts_app:src/billing/tax.ts")
    assert "class TaxCalculator" in outline
    assert "File not in index" in call("get_file_outline", path="nope.ts")


def test_add_note_is_shown_on_the_symbol(call):
    assert "added to roundMoney" in call("add_note", target="roundMoney", text="Never use floats")
    assert "note (agent): Never use floats" in call("get_symbol", name="roundMoney")


def test_reindex(call, tmp_path):
    money = tmp_path / "ts_app/src/shared/money.ts"
    money.write_text(money.read_text() + "\nexport function newHelper() { return 1; }\n")
    text = call("reindex", paths=["ts_app/src/shared/money.ts"])
    assert "ts_app: 1 indexed" in text
    assert "function newHelper" in call("get_symbol", name="newHelper")
    assert "0 indexed" in call("reindex")
