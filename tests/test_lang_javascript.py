from conftest import index_fixtures


def test_javascript_commonjs_and_esm(tmp_path):
    ws = index_fixtures(tmp_path, ["js_app"])
    found = ws.symbols()
    assert found["roundMoney"] == ("function", "Rounds an amount to cents.")
    assert found["Cart"] == ("class", "A shopping cart with line items.")
    assert found["Cart.total"][0] == "method"
    assert found["CartView"][0] == "component"
    assert found["Cart totals.adds VAT"][0] == "test"

    calls = ws.calls()
    assert calls[("Cart.total", "roundMoney")] == "roundMoney"  # require + destructuring
    assert calls[("CartView", "Cart")] == "Cart"  # import from "./cart.js"
    assert calls[("CartView", "total")] == "Cart.total"  # typed local variable
    assert calls[("Cart totals.adds VAT", "total")] == "Cart.total"
    assert calls[("Cart totals.adds VAT", "Cart")] == "Cart"  # require("../src/cart")

    with ws.engine.connect() as conn:
        from wingman.storage.schema import files

        languages = dict(
            conn.execute(files.select().with_only_columns(files.c.path, files.c.language)).all()
        )
    assert languages["src/view.jsx"] == "javascript" and languages["src/api.mjs"] == "javascript"
