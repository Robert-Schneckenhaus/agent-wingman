from wingman.extract import extract


def _extract(source: str, path: str = "a.ts"):
    return extract(path, source.encode())


def _symbols(result):
    return {s.qualified_name: s for s in result.symbols}


def test_classes_members_and_docs():
    result = _extract(
        """
/** Renders invoices. */
@Injectable()
export default class Renderer extends Base implements IRenderer {
  private calc: TaxCalculator;
  constructor(private readonly svc: InvoiceService) { super(); }
  /** Renders one invoice. */
  async render(inv: Invoice): Promise<string> { return ""; }
  get name(): string { return "x"; }
}
"""
    )
    symbols = _symbols(result)
    cls = symbols["Renderer"]
    assert cls.kind == "class"
    assert cls.modifiers == ["export", "default"]
    assert cls.decorators == ["@Injectable()"]
    assert cls.doc == "Renders invoices."
    assert symbols["Renderer.render"].kind == "method"
    assert symbols["Renderer.render"].doc == "Renders one invoice."
    assert "async" in symbols["Renderer.render"].modifiers
    assert symbols["Renderer.render"].signature == "async render(inv: Invoice): Promise<string>"
    assert symbols["Renderer.name"].kind == "property"
    assert symbols["Renderer.calc"].visibility == "private"
    assert symbols["Renderer.constructor"].kind == "constructor"

    heritage = {
        (r.kind, r.target_name) for r in result.refs if r.kind in ("inherits", "implements")
    }
    assert heritage == {("inherits", "Base"), ("implements", "IRenderer")}


def test_functions_consts_interfaces_enums_namespaces():
    result = _extract(
        """
export function sum(a: number) { return a; }
export const double = (a: number): number => a * 2;
export const RATE = 0.19;
let notExported = 1;
interface Shape { area(): number; name: string; }
enum Color { Red }
type Id = string;
namespace Utils { export function helper() {} }
"""
    )
    kinds = {s.qualified_name: s.kind for s in result.symbols}
    assert kinds == {
        "sum": "function",
        "double": "function",
        "RATE": "const",
        "Shape": "interface",
        "Shape.area": "method",
        "Shape.name": "property",
        "Color": "enum",
        "Id": "type_alias",
        "Utils": "namespace",
        "Utils.helper": "function",
    }


def test_react_components_and_jsx_refs():
    result = _extract(
        """
export function Badge() { return <span />; }
export const View = () => <div><Badge /></div>;
export function notAComponent() { return 1; }
""",
        "view.tsx",
    )
    kinds = {s.name: s.kind for s in result.symbols}
    assert kinds == {"Badge": "component", "View": "component", "notAComponent": "function"}
    jsx = [r for r in result.refs if r.kind == "instantiate"]
    assert [(r.target_name, result.symbols[r.from_symbol].name) for r in jsx] == [("Badge", "View")]


def test_imports_and_reexports():
    result = _extract(
        """
import Default, * as ns from "./a";
import { x, y as z } from "@app/b";
import "./side-effect";
export * from "./c";
export { p as q } from "./d";
"""
    )
    imports = [(i.module, i.imported_name, i.alias, i.kind) for i in result.imports]
    assert imports == [
        ("./a", "default", "Default", "import"),
        ("./a", "*", "ns", "import"),
        ("@app/b", "x", None, "import"),
        ("@app/b", "y", "z", "import"),
        ("./side-effect", None, None, "import"),
        ("./c", "*", None, "reexport"),
        ("./d", "p", "q", "reexport"),
    ]


def test_call_refs_with_receiver_types():
    result = _extract(
        """
class A {
  private calc: TaxCalculator;
  constructor(private svc: InvoiceService) {}
  run(inv: Invoice) {
    this.calc.breakdown(inv);
    this.svc.load();
    inv.total();
    const r = new Renderer();
    r.render();
    helper();
    this.other();
    console.log("x");
  }
}
"""
    )
    calls = {
        r.target_name: (r.qualifier, r.qualifier_type) for r in result.refs if r.kind == "call"
    }
    assert calls["breakdown"] == ("this.calc", "TaxCalculator")
    assert calls["load"] == ("this.svc", "InvoiceService")
    assert calls["total"] == ("inv", "Invoice")
    assert calls["render"] == ("r", "Renderer")
    assert calls["helper"] == (None, None)
    assert calls["other"] == ("this", None)
    assert calls["log"] == ("console", None)
    assert all(result.symbols[r.from_symbol].name == "run" for r in result.refs if r.kind == "call")


def test_chain_root_types_and_value_types():
    result = _extract(
        """
class A {
  private repo: InvoiceRepository;
  async load(id: number): Promise<Invoice> { return this.repo.unitOfWork.find(id); }
  run(svc: Service) { svc.inner.go(); }
}
"""
    )
    calls = {
        r.target_name: (r.qualifier, r.qualifier_type) for r in result.refs if r.kind == "call"
    }
    assert calls["find"] == ("this.repo.unitOfWork", "InvoiceRepository")
    assert calls["go"] == ("svc.inner", "Service")
    types = {s.qualified_name: s.type_name for s in result.symbols}
    assert types == {"A": None, "A.repo": "InvoiceRepository", "A.load": "Invoice", "A.run": None}


def test_type_refs_are_deduplicated_and_skip_type_parameters():
    result = _extract("function f<T>(a: Foo, b: Foo, c: T): Promise<Bar> { return null; }")
    type_refs = [r.target_name for r in result.refs if r.kind == "type_ref"]
    assert type_refs == ["Foo", "Promise", "Bar"]


def test_test_blocks_become_symbols_and_locals_stay_local():
    result = _extract(
        """
describe("TaxCalculator", () => {
  const calc = new Calc();
  it.only("applies VAT", () => {
    calc.run();
  });
  helper();
});
"""
    )
    assert [(s.kind, s.qualified_name) for s in result.symbols] == [
        ("test_suite", "TaxCalculator"),
        ("test", "TaxCalculator.applies VAT"),
    ]
    run = next(r for r in result.refs if r.target_name == "run")
    assert run.qualifier_type == "Calc"
    assert result.symbols[run.from_symbol].kind == "test"


def test_decorators_are_calls_of_the_decorated_symbol():
    result = _extract(
        """
@Component({ selector: "x" })
export class A {
  @Input() name: string;
  @Get(":id") find() {}
}
"""
    )
    calls = {
        (r.target_name, result.symbols[r.from_symbol].qualified_name)
        for r in result.refs
        if r.kind == "call"
    }
    assert calls == {("Component", "A"), ("Input", "A.name"), ("Get", "A.find")}
