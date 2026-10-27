# agent-wingman – Konzept

> Ein Code-Index und Wissensspeicher für AI Agents (Claude Code & Co.), damit ein Agent bei einem Bug Report oder einer Coding-Aufgabe in Sekunden die relevanten Stellen im Code findet, statt das Repo per `grep` und Dateilesen abzutasten.

---

## 1. Ziele und Nicht-Ziele

### Ziele

1. **Strukturindex**: Funktionen, Methoden, Klassen, Interfaces, Konstanten, Module erkennen. Für jedes Symbol speichern: wo definiert (Datei, Zeilen), Signatur, Doku.
2. **Beziehungen**: Wer ruft wen auf, wer erbt von wem, wer importiert was, wer referenziert welches Symbol.
3. **Beschreibungen**: Vorhandene Doku (Docstrings, JSDoc, XML-Doc, Kommentare) übernehmen. Fehlt sie, mit einem LLM eine kurze Beschreibung erzeugen und als *generiert* markieren.
4. **Externe Doku**: PDFs, URLs, Markdown, Confluence-Exporte usw. einlesen, in Abschnitte zerlegen und mit Symbolen und Dateien verknüpfen.
5. **Schnelle Suche für Agents**: Volltext (BM25), semantisch (Embeddings) und über den Graphen, kombiniert zu einer Rangliste. Ausgabe kompakt und für ein Kontextfenster gedacht.
6. **Austauschbare Speicherung**: SQLite als Default (eine Datei, keine Abhängigkeiten), optional PostgreSQL (mit `pgvector`) oder eine andere SQL-DB.
7. **Inkrementell**: Nach einer Änderung nur die betroffenen Dateien neu indexieren (Hash-basiert, git-aware).

### Nicht-Ziele (vorerst)

- Kein vollständiger Compiler oder Typchecker. Wir nehmen präzise Auflösung, wo es billig geht, sonst Heuristik mit Konfidenzwert.
- Keine IDE und keine Web-UI am Anfang. Primäre Konsumenten sind CLI und MCP.
- Keine Codeänderungen durch das Tool selbst.

---

## 2. Gesamtarchitektur

```
                 ┌────────────────────────────────────────────────┐
                 │                 Konsumenten                    │
                 │  Claude Code (MCP) · CLI · Python API · Hooks  │
                 └───────────────┬────────────────────────────────┘
                                 │
                       ┌─────────▼─────────┐
                       │   Query Engine    │  hybrid: FTS + Vektor + Graph
                       │  (Ranking, Pack)  │  → kompakte "Context Packs"
                       └─────────┬─────────┘
                                 │
        ┌────────────────────────▼─────────────────────────┐
        │                  Storage Layer                   │
        │  Repository-Interface → SQLite (default) │ PG    │
        │  Tabellen · FTS5 · Vektorindex · Graph-Kanten    │
        └────────────────────────▲─────────────────────────┘
                                 │
   ┌─────────────────────────────┼──────────────────────────────┐
   │                      Indexing Pipeline                     │
   │                                                            │
   │  Scanner → Parser → Extractor → Resolver → Enricher → Embed│
   │  (Dateien)  (AST)   (Symbole,   (Calls     (Doku/LLM-  (Vek-│
   │                      Refs)       auflösen)  Summaries)  toren)│
   │                                                            │
   │  Doc Ingestor: PDF · URL · MD · HTML → Chunks → Linker     │
   └────────────────────────────────────────────────────────────┘
```

**Sprache der Implementierung:** Python 3.11+.
Begründung: `tree-sitter` hat gute Python-Bindings (`tree-sitter-language-pack`), SQLite ist eingebaut, PDF/HTML-Parsing ist reif (`pymupdf`, `trafilatura`), das MCP-SDK gibt es für Python, und das LLM-/Embedding-Ökosystem ist dort am breitesten.

---

## 3. Indexing Pipeline im Detail

### 3.1 Scanner

- Läuft durch das Repo und respektiert `.gitignore` sowie eine eigene `.wingmanignore`.
- Erkennt die Sprache über Dateiendung und Shebang (optional Linguist-Heuristik).
- Berechnet pro Datei einen `content_hash` (SHA-256). Ist der Hash unverändert, wird die Datei übersprungen.
- Git-aware: liest `HEAD`-Commit und Branch und kann mit `git diff --name-only <last_indexed_commit>` nur die geänderten Dateien liefern.

### 3.2 Parser (tree-sitter)

- Einheitlicher Zugang über **tree-sitter** für alle Sprachen: robust bei Syntaxfehlern, schnell, inkrementell.
- Pro Sprache eine **Query-Datei** (`queries/<lang>/symbols.scm`, `refs.scm`), ähnlich den `tags.scm` von GitHub/nvim. Neue Sprachen kommen dadurch meist ohne neuen Python-Code dazu.
- Startsprachen: Python, TypeScript/JavaScript, C#, Java, Go. Danach C/C++, Rust, PHP, Kotlin.

### 3.3 Extractor – was pro Datei herausgezogen wird

| Element         | Inhalt                                                                 |
|-----------------|------------------------------------------------------------------------|
| Symbol          | Name, Art (`class`, `function`, `method`, `interface`, `enum`, `const`, `module`, `property`…), qualifizierter Name (`pkg.mod.Class.method`), Signatur, Sichtbarkeit, Modifier (static, async, abstract), Zeilen/Bytes von–bis, Eltern-Symbol |
| Doku            | Docstring / JSDoc / XML-Doc / führende Kommentare, roh und normalisiert |
| Referenzen      | Aufrufe, Instanziierungen, Typverwendungen, Attributzugriffe mit Position und dem *Namen*, wie er im Code steht |
| Imports         | `import`/`using`/`require` → Modulname und Alias                        |
| Vererbung       | `extends` / `implements` / Basisklassen                                 |
| Annotationen    | Decorators/Attributes (`@app.route("/x")`, `[HttpGet]`) – wichtig, um Einstiegspunkte zu finden |

### 3.4 Resolver – Referenzen auf Symbole auflösen

Das ist der schwierigste Teil. Deshalb mehrstufig, und jede Kante bekommt eine **Konfidenz**:

1. **Lokal**: Name im selben Scope oder in derselben Datei definiert → `1.0`
2. **Import-basiert**: Über die Import-Tabelle der Datei auf ein Symbol in einem anderen Modul → `0.9`
3. **Typ-Heuristik**: `self.foo()` / `this.foo()` → Methode der eigenen Klasse oder einer Basisklasse → `0.8`
4. **Name-Match global**: Eindeutiger Name im Repo → `0.5`. Mehrdeutig → mehrere Kanten mit geteilter Konfidenz.
5. **Optional präzise**: Ein Adapter nutzt LSP-Server (pyright, tsserver, OmniSharp) oder **SCIP**-Indizes (`scip-python`, `scip-typescript` …), wenn sie vorhanden sind. Diese Kanten bekommen `1.0` und ersetzen die heuristischen.

Nicht auflösbare Referenzen (z. B. auf externe Libraries) bleiben als `unresolved_name` erhalten. Sie helfen trotzdem bei der Suche, etwa „wo wird `requests.post` aufgerufen?“.

### 3.5 Enricher – Beschreibungen

- **Vorhandene Doku** wird übernommen (`doc_source = 'docstring'`).
- **Fehlt Doku**, erzeugt ein LLM eine Kurzbeschreibung (1–2 Sätze plus optionale Stichworte), `doc_source = 'generated'`.
  - Input: Signatur, Body (gekürzt), Namen der Callees, Klassenkontext.
  - Bottom-up: zuerst Blätter des Call-Graphs, damit Callers die Summaries ihrer Callees als Kontext nutzen können.
  - **Datei- und Modul-Summaries** werden aus den Symbol-Summaries aggregiert. Optional gibt es eine Repo-Summary (Architekturüberblick).
- **Caching**: Der Schlüssel ist `hash(body + model + prompt_version)`. Unveränderter Code wird nie erneut zusammengefasst.
- **Steuerbar**: `--no-llm`, Budget-Limit (max. Tokens oder Kosten pro Lauf), Filter (z. B. nur public Symbole, nur ab N Zeilen).
- **Provider-agnostisch**: Anthropic API als Default, OpenAI-kompatibel oder lokal (Ollama) über ein schmales Interface.

### 3.6 Embeddings

- Embeddings für Symbole (Name + Signatur + Doku/Summary + gekürzter Body), Datei-Summaries und Doku-Chunks.
- Lokal per Default (`sentence-transformers`, z. B. ein Code-fähiges Modell wie `jina-embeddings-v2-base-code` oder `bge-small`), damit Code das Haus nicht verlässt. Alternativ über eine API.
- Gespeichert als Vektor plus `model`-Name. Ein Modellwechsel macht einen Re-Embed-Lauf nötig, und das ist erkennbar.

### 3.7 Doc Ingestor – externe Doku

| Quelle      | Werkzeug                                   |
|-------------|--------------------------------------------|
| PDF         | `pymupdf` (Text + Struktur), OCR optional  |
| URL / HTML  | `httpx` + `trafilatura` (Hauptinhalt)      |
| Markdown/RST| direkt, an Überschriften gesplittet        |
| OpenAPI     | Endpunkte als eigene Chunks                |

Ablauf:
1. Laden, in **Chunks** an Struktur­grenzen zerlegen (Überschrift/Abschnitt, ~300–800 Tokens). Seite oder Anker wird mitgespeichert, damit die Quelle zitierbar ist.
2. **Linking** zu Code:
   - *Explizit*: Der User gibt beim Hinzufügen Pfade oder Symbole an (`wingman doc add spec.pdf --link src/billing/`).
   - *Automatisch*: Bezeichner im Text (`InvoiceService`, `calc_tax()`) werden gegen die Symbolnamen gematcht. Zusätzlich gibt es Kandidaten über Embedding-Ähnlichkeit oberhalb einer Schwelle, gespeichert mit Konfidenz und `link_source`.
3. Re-Fetch von URLs per `ETag`/`Last-Modified` oder Content-Hash.

---

## 4. Datenmodell

Ziel: eine **relationale Kernstruktur**, die in SQLite und PostgreSQL gleich aussieht. Graph-Abfragen laufen über Kantentabellen und rekursive CTEs, beides können beide DBs.

### 4.1 Übersicht

```
repositories 1─n index_runs
repositories 1─n files 1─n symbols ─┐ (parent_id: Verschachtelung)
                      │             │
                      └─1─n refs n──┘ (from_symbol → to_symbol, + unresolved_name)
files 1─n imports
symbols n─n symbols   via symbol_edges (inherits, implements, overrides)
documents 1─n doc_chunks n─n (symbols|files) via doc_links
summaries / embeddings  (polymorph an symbol | file | doc_chunk)
```

### 4.2 Tabellen (SQL, vereinfacht)

```sql
-- Ein indexiertes Repository (mehrere pro DB möglich, z. B. Monorepo + Libs)
CREATE TABLE repositories (
  id              INTEGER PRIMARY KEY,
  name            TEXT NOT NULL,
  root_path       TEXT NOT NULL,
  remote_url      TEXT,
  last_commit     TEXT,              -- zuletzt indexierter Git-Commit
  created_at      TIMESTAMP, updated_at TIMESTAMP
);

CREATE TABLE index_runs (
  id INTEGER PRIMARY KEY, repo_id INTEGER REFERENCES repositories(id),
  started_at TIMESTAMP, finished_at TIMESTAMP,
  commit_sha TEXT, files_changed INTEGER, status TEXT, stats_json TEXT
);

CREATE TABLE files (
  id            INTEGER PRIMARY KEY,
  repo_id       INTEGER NOT NULL REFERENCES repositories(id),
  path          TEXT NOT NULL,       -- relativ zum Repo-Root
  language      TEXT,
  content_hash  TEXT NOT NULL,
  size_bytes    INTEGER, line_count INTEGER,
  is_test       BOOLEAN DEFAULT 0,   -- Heuristik: tests/, *_test.go, *.spec.ts …
  is_generated  BOOLEAN DEFAULT 0,
  last_modified TIMESTAMP,
  UNIQUE (repo_id, path)
);

CREATE TABLE symbols (
  id              INTEGER PRIMARY KEY,
  file_id         INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  parent_id       INTEGER REFERENCES symbols(id) ON DELETE CASCADE,
  kind            TEXT NOT NULL,     -- class|function|method|interface|enum|const|module|property|...
  name            TEXT NOT NULL,
  qualified_name  TEXT NOT NULL,     -- pkg.module.Class.method
  signature       TEXT,
  visibility      TEXT,              -- public|private|protected|internal
  modifiers       TEXT,              -- JSON: ["static","async"]
  decorators      TEXT,              -- JSON: ["@app.route('/x')"]
  start_line INTEGER, end_line INTEGER, start_byte INTEGER, end_byte INTEGER,
  body_hash       TEXT,              -- für Summary-Cache & Change-Detection
  doc             TEXT,              -- Original-Doku (normalisiert)
  summary         TEXT,              -- beste verfügbare Kurzbeschreibung
  summary_source  TEXT,              -- docstring|generated|manual
  complexity      INTEGER,           -- optional: zyklomatisch / LOC
  stable_key      TEXT NOT NULL      -- repo+path+qualified_name+kind → überlebt Re-Index
);
CREATE INDEX ix_symbols_name ON symbols(name);
CREATE INDEX ix_symbols_qname ON symbols(qualified_name);
CREATE UNIQUE INDEX ix_symbols_stable ON symbols(stable_key);

-- Aufrufe / Referenzen
CREATE TABLE refs (
  id              INTEGER PRIMARY KEY,
  from_symbol_id  INTEGER REFERENCES symbols(id) ON DELETE CASCADE, -- NULL = Top-Level der Datei
  from_file_id    INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  to_symbol_id    INTEGER REFERENCES symbols(id) ON DELETE SET NULL,
  unresolved_name TEXT,              -- falls nicht auflösbar (z. B. externe Lib)
  kind            TEXT NOT NULL,     -- call|instantiate|type_ref|read|write|import
  line INTEGER, col INTEGER,
  confidence      REAL NOT NULL DEFAULT 1.0,
  resolver        TEXT               -- local|import|heuristic|name|scip|lsp
);
CREATE INDEX ix_refs_to   ON refs(to_symbol_id);
CREATE INDEX ix_refs_from ON refs(from_symbol_id);

CREATE TABLE symbol_edges (            -- strukturelle Beziehungen
  src_id INTEGER REFERENCES symbols(id) ON DELETE CASCADE,
  dst_id INTEGER REFERENCES symbols(id) ON DELETE CASCADE,
  kind   TEXT NOT NULL,                -- inherits|implements|overrides|tests
  confidence REAL DEFAULT 1.0,
  PRIMARY KEY (src_id, dst_id, kind)
);

CREATE TABLE imports (
  file_id INTEGER REFERENCES files(id) ON DELETE CASCADE,
  module TEXT NOT NULL, alias TEXT, imported_name TEXT,
  resolved_file_id INTEGER REFERENCES files(id), line INTEGER
);

-- Externe Dokumentation
CREATE TABLE documents (
  id INTEGER PRIMARY KEY, repo_id INTEGER REFERENCES repositories(id),
  source_type TEXT,          -- pdf|url|markdown|html|openapi
  uri TEXT NOT NULL, title TEXT, content_hash TEXT,
  fetched_at TIMESTAMP, metadata_json TEXT
);

CREATE TABLE doc_chunks (
  id INTEGER PRIMARY KEY,
  document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
  ordinal INTEGER, heading_path TEXT,   -- "Kapitel 3 > Rechnungen > Steuern"
  locator TEXT,                          -- Seite 12 / #anchor
  text TEXT NOT NULL, token_count INTEGER
);

CREATE TABLE doc_links (
  chunk_id INTEGER REFERENCES doc_chunks(id) ON DELETE CASCADE,
  target_type TEXT NOT NULL,     -- symbol|file|directory
  target_id INTEGER, target_path TEXT,
  link_source TEXT,              -- manual|name_match|semantic
  confidence REAL DEFAULT 1.0
);

-- Embeddings (polymorph)
CREATE TABLE embeddings (
  owner_type TEXT NOT NULL,      -- symbol|file|doc_chunk
  owner_id   INTEGER NOT NULL,
  model      TEXT NOT NULL,
  dim        INTEGER NOT NULL,
  vector     BLOB NOT NULL,      -- float32[]; in PG: vector(dim)
  text_hash  TEXT,
  PRIMARY KEY (owner_type, owner_id, model)
);

-- LLM-Summary-Cache (unabhängig von Symbol-IDs, überlebt Re-Index)
CREATE TABLE summary_cache (
  key TEXT PRIMARY KEY,          -- hash(body_hash + model + prompt_version)
  summary TEXT, keywords TEXT, model TEXT, created_at TIMESTAMP
);

-- Freie Notizen / Wissen vom Agent oder Menschen ("Gotchas")
CREATE TABLE notes (
  id INTEGER PRIMARY KEY, target_type TEXT, target_id INTEGER, target_path TEXT,
  author TEXT, text TEXT, created_at TIMESTAMP
);
```

### 4.3 Volltextsuche

- **SQLite**: `FTS5`-Virtual-Table über `symbols(name, qualified_name, signature, doc, summary)`, `doc_chunks(heading_path, text)` und optional Datei-Inhalt. Tokenizer `unicode61` mit `tokenchars '_'`, dazu eine eigene Vorverarbeitung, die `camelCase`/`snake_case` splittet, damit `InvoiceService` auch bei „invoice service“ trifft.
- **PostgreSQL**: `tsvector` + GIN-Index, gleiche Vorverarbeitung.

### 4.4 Vektorsuche

- **SQLite**: Extension [`sqlite-vec`](https://github.com/asg017/sqlite-vec). Fallback ohne Extension: Vektoren als BLOB laden und Brute-Force mit numpy. Bis ca. 100k Symbole ist das schnell genug.
- **PostgreSQL**: `pgvector` mit HNSW-Index.

### 4.5 Austauschbare Datenbank

- **SQLAlchemy Core** (nicht ORM) für Schema und Queries plus **Alembic** für Migrationen. Damit laufen SQLite, PostgreSQL, MySQL/MariaDB über dieselbe Codebasis.
- DB-spezifische Features (FTS, Vektor) liegen hinter einem kleinen **Backend-Interface**:

```python
class SearchBackend(Protocol):
    def fts_search(self, query: str, kinds: set[str], limit: int) -> list[Hit]: ...
    def vector_search(self, vec: np.ndarray, owner_types: set[str], limit: int) -> list[Hit]: ...
    def upsert_embeddings(self, rows: Iterable[EmbeddingRow]) -> None: ...
```

  Implementierungen: `SqliteBackend` (FTS5 + sqlite-vec), `PostgresBackend` (tsvector + pgvector). Weitere sind möglich, z. B. eine externe Vektor-DB wie Qdrant neben einer beliebigen SQL-DB.
- Konfiguration per URL: `WINGMAN_DB=sqlite:///.wingman/index.db` (Default) oder `postgresql://…`.

---

## 5. Retrieval – wie ein Agent schnell das Richtige findet

### 5.1 Hybrid-Suche

Für eine Anfrage wie einen Bug Report („Beim Export als PDF fehlt bei Rechnungen mit Rabatt die MwSt-Zeile“):

1. **Query-Aufbereitung**: Bezeichner, Dateipfade, Fehlermeldungen und Stacktrace-Frames extrahieren (Regex). Optional erzeugt ein LLM Suchbegriffe und Synonyme.
2. **Kandidaten** aus drei Quellen:
   - **Exakt**: Stacktrace-Frames und genannte Symbole/Dateien. Höchste Priorität.
   - **FTS/BM25** über Namen, Doku, Summaries, Doku-Chunks.
   - **Semantisch**: Embedding der Anfrage gegen Symbol-, Datei- und Chunk-Vektoren.
3. **Fusion** per *Reciprocal Rank Fusion* (RRF).
4. **Graph-Expansion**: Für die Top-N die direkten Callers und Callees (1–2 Hops), Basisklassen, zugehörige Tests und verlinkte Doku-Chunks dazunehmen, mit abklingendem Score.
5. **Re-Ranking** (optional): ein Cross-Encoder oder LLM-Rerank der Top-30.
6. **Context Pack** bauen (siehe unten), mit Token-Budget.

### 5.2 Context Pack – Ausgabeformat für Agents

Kompakt, zitierbar, mit Pfad und Zeilen, damit der Agent gezielt `Read` aufrufen kann:

```
## Relevante Symbole (Top 8 von 41)

1. billing/export/pdf_renderer.py:112-168  PdfRenderer.render_tax_lines(invoice)
   ↳ Rendert MwSt-Zeilen pro Steuersatz. [docstring]
   called by: PdfRenderer.render (pdf_renderer.py:54) · calls: TaxCalculator.breakdown
   tests: tests/export/test_pdf_renderer.py::test_tax_lines
2. billing/tax.py:40-95  TaxCalculator.breakdown(items, discount=None)
   ↳ Gruppiert Positionen nach Steuersatz; Rabatt wird vorher abgezogen. [generated]
   ...

## Verknüpfte Doku
- Spezifikation_Rechnung_v3.pdf, S. 12 „Rabatte und Steuer“ → TaxCalculator.breakdown
```

Optionen: `--with-code` (Body inline, gekürzt), `--budget 4000` (Tokens), `--format json|markdown`.

---

## 6. Schnittstellen

### 6.1 CLI (`wingman`)

```bash
wingman init                         # .wingman/ anlegen, Config, DB
wingman index [--incremental] [--no-llm] [--lang py,ts]
wingman watch                        # Dateisystem-Watcher → inkrementell
wingman search "pdf export tax"      # Hybrid-Suche → Context Pack
wingman symbol PdfRenderer.render    # Definition, Doku, Callers, Callees
wingman callers TaxCalculator.breakdown --depth 2
wingman callees ... | wingman refs ... | wingman outline path/to/file.py
wingman doc add spec.pdf --link src/billing/
wingman doc add https://docs.example.com/api --crawl-depth 1
wingman note add TaxCalculator.breakdown "Rabatt muss vor Steuer abgezogen werden (Ticket #123)"
wingman stats | wingman doctor
wingman serve --mcp                  # MCP-Server (stdio oder HTTP)
```

### 6.2 MCP-Server (Hauptintegration für Claude Code)

| Tool                   | Zweck                                                         |
|------------------------|---------------------------------------------------------------|
| `search_code`          | Hybrid-Suche mit Freitext, liefert ein Context Pack            |
| `find_relevant_for_task` | Nimmt einen Bug Report oder eine Aufgabe, liefert Context Pack plus vermutete Einstiegspunkte |
| `get_symbol`           | Definition, Signatur, Doku, Ort, optional Code                |
| `get_callers` / `get_callees` | Call-Graph mit Tiefe                                  |
| `get_file_outline`     | Symbolbaum einer Datei ohne Bodies                            |
| `find_tests_for`       | Tests, die ein Symbol (indirekt) aufrufen                     |
| `search_docs`          | Nur externe Doku durchsuchen                                  |
| `add_note`             | Agent hinterlegt Erkenntnisse für spätere Sessions            |
| `reindex_files`        | Nach Änderungen gezielt neu indexieren                        |

Einbindung: `claude mcp add wingman -- wingman serve --mcp`.

### 6.3 Claude-Code-Integration darüber hinaus

- **Hooks**: Ein `PostToolUse`-Hook auf `Edit`/`Write` ruft `wingman reindex <file>` auf. Ein `SessionStart`-Hook führt `wingman index --incremental` aus.
- **Skill / CLAUDE.md-Snippet**: kurze Anleitung „Nutze zuerst `search_code`/`find_relevant_for_task`, bevor du großflächig grep-st“.
- **Python-API** für eigene Skripte und andere Agent-Frameworks.

---

## 7. Projektstruktur (geplant)

```
agent-wingman/
├── pyproject.toml
├── README.md
├── CONCEPT.md
├── src/wingman/
│   ├── cli.py
│   ├── config.py               # .wingman/config.toml + Env
│   ├── scanner.py
│   ├── parsing/
│   │   ├── treesitter.py
│   │   └── queries/<lang>/{symbols,refs}.scm
│   ├── extract/                # Symbol-, Ref-, Import-Extraktion
│   ├── resolve/                # Resolver-Stufen, SCIP/LSP-Adapter
│   ├── enrich/                 # LLM-Summaries, Provider-Interface
│   ├── embed/                  # Embedding-Provider
│   ├── docs/                   # PDF/URL/MD-Ingest, Chunker, Linker
│   ├── storage/
│   │   ├── schema.py           # SQLAlchemy Core Tabellen
│   │   ├── migrations/         # Alembic
│   │   ├── sqlite_backend.py
│   │   └── postgres_backend.py
│   ├── query/                  # Hybrid-Suche, RRF, Graph-Expansion, Context Pack
│   └── mcp_server.py
└── tests/
    └── fixtures/               # Mini-Repos je Sprache mit erwarteten Indizes
```

Lokaler Zustand pro Projekt in `.wingman/` (DB, Cache, Config). Kommt in `.gitignore`, optional teilbar.

---

## 8. Roadmap

| Phase | Inhalt | Ergebnis |
|------|--------|----------|
| **0 – Skeleton** | `pyproject`, CLI-Gerüst, Config, SQLite-Schema + Alembic | `wingman init` läuft |
| **1 – Strukturindex** | Scanner, tree-sitter für Python + TS, Symbole, Imports, Refs, lokaler/Import-Resolver, FTS5 | `wingman symbol`, `callers`, `outline`, `search` (nur FTS) |
| **2 – MCP** | MCP-Server mit Kern-Tools, Context Pack, inkrementelles Indexing, Hooks | In Claude Code nutzbar |
| **3 – Semantik** | Embeddings (lokal), sqlite-vec, Hybrid-Suche + RRF + Graph-Expansion | Bug-Report → relevante Stellen |
| **4 – Summaries** | LLM-Enricher bottom-up, Cache, Budget, Datei-/Modul-Summaries | Undokumentierter Code wird durchsuchbar |
| **5 – Externe Doku** | PDF/URL/MD-Ingest, Chunking, Auto-Linking | Spezifikationen hängen am Code |
| **6 – Breite** | Weitere Sprachen (C#, Java, Go …), SCIP-Adapter, PostgreSQL-Backend, Notes | Produktionsreif für größere Codebasen |
| **7 – Evaluation** | Benchmark: echte Issues/Commits aus OSS-Repos, Messung Recall@k der geänderten Dateien | Messbare Qualität, Tuning |

---

## 9. Qualität messen

Die gute Nachricht: Git-Historien liefern Ground Truth gratis. Für jeden Bugfix-Commit oder jede Issue-PR-Kombination gilt: Issue-Text als Query, die im Fix geänderten Dateien und Symbole als erwartete Treffer. Kennzahlen:

- **Recall@k** (Datei- und Symbolebene), **MRR**
- **Token-Effizienz**: Wie viele Tokens braucht der Agent bis zur ersten richtigen Datei, mit und ohne wingman?
- Indexing-Dauer und DB-Größe pro 100k LOC

---

## 10. Offene Fragen / Entscheidungen

1. **Embedding-Modell**: lokal (Datenschutz, kostenlos, schwächer) oder API (besser, Kosten, Code verlässt das Haus)? Vorschlag: lokal als Default, API optional.
2. **LLM-Summaries**: Welcher Provider als Default, und mit welchem Kostenlimit? Vorschlag: Anthropic, ein günstiges kleines Modell, Opt-in per Config.
3. **Multi-Repo**: Ein Index über mehrere Repos (Service-Landschaft) vom Start weg? Das Schema erlaubt es bereits über `repo_id`.
4. **Sprachen-Priorität**: Welche Sprachen nutzt du am meisten? Davon hängt die Reihenfolge in Phase 1 und 6 ab.
5. **Teilen des Index**: Lokal pro Entwickler oder zentral, z. B. per CI gebaut und als Artefakt oder Postgres bereitgestellt?
