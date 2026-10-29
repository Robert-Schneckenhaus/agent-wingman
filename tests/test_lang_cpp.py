from conftest import index_fixtures
from sqlalchemy import select

from wingman.extract import extract
from wingman.storage.schema import refs, symbols


def test_cpp_declarations():
    source = b"""
namespace shop {
/// A repository.
class Repo : public Base {
public:
  Repo();
  /** Finds an order. */
  Order* find(int id) const;
  virtual void save(const Order& order) = 0;
private:
  Store* store_;
};
Order* Repo::find(int id) const { store_->load(id); return nullptr; }
}
"""
    result = extract("repo.cpp", source)
    found = [(s.kind, s.qualified_name, s.visibility, s.modifiers) for s in result.symbols]
    assert found == [
        ("class", "shop.Repo", None, []),
        ("constructor", "shop.Repo.Repo", "public", ["declaration"]),
        ("method", "shop.Repo.find", "public", ["declaration"]),
        ("method", "shop.Repo.save", "public", ["virtual", "declaration", "abstract"]),
        ("field", "shop.Repo.store_", "private", []),
        ("method", "shop.Repo.find", None, []),
    ]
    assert result.symbols[0].doc == "A repository."
    assert result.symbols[2].doc == "Finds an order."
    assert result.symbols[5].parent == 0  # out-of-line definition belongs to the class
    load = next(r for r in result.refs if r.target_name == "load")
    assert (load.qualifier, load.qualifier_type) == ("store_", "Store")


def test_cpp_and_c_resolution(tmp_path):
    ws = index_fixtures(tmp_path, ["cpp_app"])
    found = ws.symbols()
    assert found["shop.Order"] == ("class", "A customer order.")
    assert found["shop.util.round"][0] == "function"

    calls = ws.calls()
    # member via a typed pointer field; the call goes to the definition in order.cpp
    assert calls[("shop.OrderService.cancelOrder", "find")] == "shop.OrderRepository.find"
    assert calls[("shop.OrderService.cancelOrder", "cancel")] == "shop.Order.cancel"
    # namespace-qualified call from an out-of-line member definition
    assert calls[("shop.Order.total", "round")] == "shop.util.round"
    # C: prototype in a header -> definition; static function and macro in the file
    assert calls[("main", "validate")] == "validate"
    assert calls[("validate", "checksum")] == "checksum"
    assert calls[("validate", "CHECK_LEN")] == "CHECK_LEN"

    target = symbols.alias()
    with ws.engine.connect() as conn:
        rows = conn.execute(
            select(target.c.modifiers)
            .select_from(refs)
            .join(target, target.c.id == refs.c.to_symbol_id)
            .where(refs.c.target_name.in_(["validate", "cancel", "round"]), refs.c.kind == "call")
        ).all()
    assert rows and all("declaration" not in (r[0] or []) for r in rows)
