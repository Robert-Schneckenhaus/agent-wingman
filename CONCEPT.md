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
- Pro Sprache ein **Extractor in Python**, der den tree-sitter-Baum durchläuft (`src/wingman/extract/<lang>.py`).
  > *Umgesetzt in Phase 1:* Ursprünglich waren `.scm`-Query-Dateien geplant, ähnlich den `tags.scm` von GitHub/nvim. Für qualifizierte Namen, Verschachtelung, Doku-Kommentare und die Typinferenz von Empfängern (`_calc.Breakdown()` → `ITaxCalculator`) braucht es aber Kontext über mehrere Knoten. Mit einem Visitor geht das deutlich einfacher. Neue Sprachen kosten damit etwas Python-Code, je nach Sprache ca. 400–500 Zeilen.
- **Sprachpriorität:**
  1. **TypeScript** (inkl. TSX) und **C#**: Prototyp
  2. **JavaScript** (inkl. JSX): fällt fast gratis mit TS ab, gleiche Grammatik-Familie
  3. **Java**, **Python**, **PHP**
  4. **C und C++**: fest eingeplant, direkt nach Stufe 3
  5. später nach Bedarf: Go, Kotlin, Rust, Swift …
- Sprachspezifische Besonderheiten, die der Extractor kennen muss:
  - **C#**: `namespace`/file-scoped namespaces, `partial class` (ein Symbol über mehrere Dateien), Properties mit Getter/Setter, Extension Methods, Attribute (`[HttpGet("x")]`, `[Fact]`/`[Test]` für Testerkennung), XML-Doc-Kommentare (`/// <summary>`), Interfaces und DI-Registrierungen (`services.AddScoped<IFoo, Foo>()` → Kante *implements/bound_to*).
  - **TypeScript**: `export`/`export default`/Re-Exports (`export * from`), Barrel-Dateien (`index.ts`), Pfad-Aliase aus `tsconfig.json` (`paths`, `baseUrl`), Arrow Functions als `const foo = () => …`, Klassen-Felder, Decorators (Angular/NestJS), JSDoc/TSDoc, React-Komponenten (Funktion, die JSX zurückgibt → `kind = component`).
  - **PHP**: Namespaces + `use`, Composer-Autoload (PSR-4) für Import-Auflösung, PHPDoc.
  - **C/C++**: Hier liegen die meisten Fallstricke.
    - **Deklaration und Definition** sind getrennt (Header `.h/.hpp` und Source `.c/.cpp`). Ein Symbol bekommt deshalb mehrere Orte (`symbol_locations` mit `role = declaration|definition`), und die Suche liefert beide.
    - **`#include`-Auflösung** über `compile_commands.json` (CMake, Bear, Meson) bzw. konfigurierbare Include-Pfade. Ohne diese Angaben gilt eine Heuristik über Dateinamen.
    - **Präprozessor:** Makros werden als eigene Symbole (`kind = macro`) erfasst. Code in `#ifdef`-Zweigen wird indexiert, aber mit der Bedingung markiert, statt verworfen zu werden.
    - **C++-Spezifika:** Namespaces, Klassen und Structs, Überladungen (die Signatur gehört zum `stable_key`), Templates, `virtual`/`override` (→ `symbol_edges` vom Typ `overrides`), Operatoren und Out-of-Class-Definitionen (`void Foo::bar() {…}` → Methode von `Foo`).
    - **Doku:** Doxygen-Kommentare (`/** */`, `///`, `//!`), wahlweise am Header oder an der Implementierung.
    - **Indirekte Aufrufe** (Funktionszeiger, Callbacks, virtuelle Aufrufe) lassen sich nur heuristisch auflösen und bekommen eine niedrige Konfidenz.
- **Präzise Auflösung (optional, Phase 7):** für C# ein kleiner Roslyn-basierter Helfer (`dotnet tool`) bzw. `scip-dotnet`, für TS/JS `scip-typescript` bzw. die TypeScript Compiler API, für C/C++ `scip-clang` bzw. clangd/libclang auf Basis von `compile_commands.json`. Gerade bei C++ (Templates, Überladungen, Makros) bringt das am meisten. Tree-sitter bleibt die Basis, die immer funktioniert, auch ohne Build.

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
   - *Umgesetzt:* Der Extractor merkt sich die deklarierten Typen von Feldern, Properties, Parametern und lokalen Variablen. Bei `_calc.Breakdown()` wird so `ITaxCalculator.Breakdown` gefunden (`0.85`).
   - Ketten wie `_repository.UnitOfWork.SaveEntitiesAsync()` werden Glied für Glied über den deklarierten Werttyp der Member (`symbols.type_name`, `Task<T>`/`Promise<T>` ausgepackt) verfolgt (`0.8`). Bei C# funktioniert das auch mit implizitem `this`.
   - Implementierungen erben die Aufrufe ihrer Interface-/Basismethode über `overrides`-Kanten. `wingman callers` zeigt sie mit „via …“ an.
4. **Name-Match global**: Eindeutiger Name im Repo → `0.5`. Mehrdeutige Namen bleiben vorerst unaufgelöst. Bekannte Laufzeit-Globals (`console`, `Math` …) und externe Typen werden nie auf Repo-Symbole gemappt.
5. **Optional präzise**: Ein Adapter nutzt LSP-Server (pyright, tsserver, OmniSharp) oder **SCIP**-Indizes (`scip-python`, `scip-typescript` …), wenn sie vorhanden sind. Diese Kanten bekommen `1.0` und ersetzen die heuristischen.

Jede Referenz speichert den Namen, wie er im Code steht (`target_name`), sowie den Empfänger (`qualifier`, `qualifier_type`). Der Resolver läuft nach jedem Index-Lauf über das ganze Repo, weil sich die Symbol-IDs geänderter Dateien ändern und Referenzen *in* diese Dateien neu aufgelöst werden müssen. Das sind reine Dictionary-Lookups im Speicher, ca. 1–2 s für 80k Referenzen. Nicht auflösbare Referenzen (z. B. auf externe Libraries) bleiben mit `target_name` erhalten. Sie helfen trotzdem bei der Suche, etwa „wo wird `requests.post` aufgerufen?“.

### 3.5 Enricher – Beschreibungen

- **Vorhandene Doku** wird übernommen (`doc_source = 'docstring'`).
- **Fehlt Doku**, erzeugt ein LLM eine Kurzbeschreibung (1–2 Sätze plus optionale Stichworte), `doc_source = 'generated'`.
  - Input: Signatur, Body (gekürzt), Namen der Callees, Klassenkontext.
  - Bottom-up: zuerst Blätter des Call-Graphs, damit Callers die Summaries ihrer Callees als Kontext nutzen können.
  - **Datei- und Modul-Summaries** werden aus den Symbol-Summaries aggregiert. Optional gibt es eine Repo-Summary (Architekturüberblick).
- **Caching**: Der Schlüssel ist `hash(body + model + prompt_version)`. Unveränderter Code wird nie erneut zusammengefasst.
- **Steuerbar**: `--no-llm`, Budget-Limit (max. Tokens oder Kosten pro Lauf), Filter (z. B. nur public Symbole, nur ab N Zeilen).
- **Provider-agnostisch**: Anthropic API als Default, OpenAI-kompatibel oder lokal (Ollama) über ein schmales Interface.

> *Umgesetzt (Phase 5):* `src/wingman/summaries.py`, Befehl `wingman summarize`.
> - **Opt-in:** `[summaries] enabled = true` in der Config, sonst verweigert `wingman summarize` den Lauf. `--dry-run` zählt Kandidaten und schätzt die Tokens, ohne etwas zu senden.
> - **Modell:** `claude-haiku-5-5` über das offizielle Anthropic-SDK (`pip install 'agent-wingman[summaries]'`), `effort: low`, strukturierte Ausgabe per JSON-Schema. Zugangsdaten wie beim SDK üblich (`ANTHROPIC_API_KEY` u. a.).
> - **Kandidaten:** Klassen, Interfaces, Structs, Records, Funktionen, Methoden und Komponenten ohne Doku-Kommentar. Test- und generierte Dateien werden ausgelassen, Funktionen und Methoden erst ab `min_lines` (Standard 3) Zeilen.
> - **Bottom-up:** Reihenfolge nach Aufruf- und Member-Graph (Tarjan für Zyklen). Ein Prompt enthält die bekannten Beschreibungen der aufgerufenen Symbole, eine Klasse die ihrer Member. Bis zu 8 Symbole pro Anfrage, 4 Anfragen parallel.
> - **Budget:** `max_tokens_per_run` (Standard 1 Mio. Input- plus Output-Tokens) oder `--max-tokens`; `--limit` begrenzt die Zahl der Symbole. Ein abgebrochener Lauf setzt beim nächsten Mal dort fort.
> - **Cache:** `summary_cache` mit Schlüssel `sha256(prompt_version + model + body_hash)`. Nach einer Re-Indexierung holen `wingman index`, `reindex` und die Hooks die Beschreibungen kostenlos aus dem Cache zurück.
> - **Wirkung:** Die Beschreibung landet in `symbols.summary` (`summary_source = 'generated'`), samt Stichworten im Volltextindex und als Doku-Teil im Embedding-Text. `wingman symbol` zeigt sie als „summary (generated)“, Context Packs zeigen sie unter dem Symbol.
> - **Geschätzte Kosten** (Dry-Run auf CleanArchitecture + eShop + nest): 4.881 Symbole, ca. 1,5 Mio. Tokens in 627 Anfragen. Mit Haiku 5.5 (0,10 $ / 0,50 $ pro Mio. Input/Output) sind das grob 0,30 $. Noch nicht mit der echten API gemessen, weil in der Entwicklungsumgebung kein API-Key vorhanden ist.
> - **Noch offen** (siehe Todo in Abschnitt 8): Datei- und Modul-Summaries, Batches-API (50 % günstiger), weitere Provider.

### 3.6 Embeddings

- Embeddings für Symbole (Name + Signatur + Doku/Summary + gekürzter Body), Datei-Summaries und Doku-Chunks.
- Lokal per Default (`sentence-transformers`, z. B. ein Code-fähiges Modell wie `jina-embeddings-v2-base-code` oder `bge-small`), damit Code das Haus nicht verlässt. Alternativ über eine API.
- Gespeichert als Vektor plus `model`-Name. Ein Modellwechsel macht einen Re-Embed-Lauf nötig, und das ist erkennbar.

> *Umgesetzt (Phase 4, vorgezogen):* `src/wingman/embeddings.py`.
> - **Provider:** `fastembed` (ONNX, ohne PyTorch, ca. 65 MB) mit `BAAI/bge-small-en-v1.5` als Standard, optional über `pip install 'agent-wingman[embeddings]'`. Das Modell wird beim ersten Gebrauch von Hugging Face geladen. Weitere Provider (API, Code-Modelle) lassen sich über das `Embedder`-Protokoll ergänzen.
> - **Konfiguration:** `[embeddings] enabled = "auto" | true | false`, `model`, `cache_dir`. Override per `WINGMAN_EMBEDDINGS=on|off|auto`.
> - **Embedding-Text pro Symbol:** Art, qualifizierter Name, Name in Wörtern, Signatur, Doku, Pfad und Anfang des Bodies. Klassen bekommen statt des Bodys ihre Member-Namen. Maximal ca. 2.000 Zeichen.
> - **Was eingebettet wird:** Klassen, Interfaces, Methoden, Funktionen, Komponenten und Tests. Felder, Properties und Konstanten nicht, sie werden über ihre Klasse gefunden.
> - **Inhaltsadressiert:** Der Indexer speichert nur `symbols.embed_hash`, die Vektoren liegen in `embedding_cache(model, text_hash)`. Unveränderter Code wird nie zweimal eingebettet, auch nicht nach `--full`-Re-Indexierung oder auf einem anderen Branch. Das ist teilbar wie in Abschnitt 10.
> - **Wann eingebettet wird:**
>   - am Ende von `wingman index` und mit `wingman embed`,
>   - im SessionStart-Hook mit max. 60 s,
>   - vor einer semantischen Abfrage für fehlende Symbole mit max. 10 s.
>   Der Post-Edit-Hook bettet nicht ein, damit er schnell bleibt.
> - **Vektorsuche:** Brute-Force mit numpy über die normalisierten Vektoren, im Prozess gecacht, solange sich der Index nicht ändert. Gemessen: 19.500 Symbole, erste Abfrage 0,2 s, danach unter 0,05 s (ohne Modell-Inferenz). `sqlite-vec`/`pgvector` lohnen sich erst bei deutlich mehr Symbolen.
> - **Fehlt das Modell** (offline, Download blockiert), degradiert die Suche zur reinen Volltextsuche mit Warnung statt Fehler.
> - **Durchsatz beim Einbetten:** Die Texte werden nach Länge sortiert in Batches gegeben, sonst wird jeder Text auf den längsten im Batch aufgefüllt. Gemessen auf 4 CPU-Kernen mit Texten von durchschnittlich ca. 190 Tokens: unsortiert 6, sortiert 17 Texte/s.
> - **Sprache:** `bge-small-en-v1.5` ist ein englisches Modell. Deutsche Bug-Reports zu englischem Code findet es praktisch nicht. Mehrsprachige Modelle wie `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` lassen sich über `model` konfigurieren. Im Stichprobentest auf 706 eShop-Symbolen fand dieses Modell deutsche Anfragen („Artikel aus dem Warenkorb entfernen“ → `CustomerBasket.RemoveItemFromBasket`), lag bei englischen Anfragen aber deutlich hinter `bge-small`. Deshalb bleibt `bge-small` der Standard.

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

> *Umgesetzt (Phase 6):* `src/wingman/docs.py`, Befehle `wingman doc add|list|search|show|remove|refresh`, MCP-Tools `search_docs`, `read_doc`, `add_doc`.
> - **Quellen:** Markdown, Text/RST, HTML (lokal oder URL, nur `<main>`/`<article>`, ohne Navigation und Skripte; Parser aus der Standardbibliothek statt `trafilatura`), PDF über `pypdf` statt `pymupdf` (leichter; Extra `docs`; Lesezeichen werden zu Überschriften), OpenAPI JSON/YAML. Verzeichnisse werden rekursiv eingelesen, ohne HTML (in Quellbäumen sind das App-Templates) und ohne JSON/YAML, das keine OpenAPI-Spezifikation ist.
> - **Chunks:** an Überschriften (Code-Blöcke werden beachtet), PDF-Seiten oder OpenAPI-Operationen, lange Abschnitte an Absätzen auf ca. 2.000 Zeichen. Locator: Anker (`#reduced-rates`), `page 3` oder `GET /api/invoices/{id}/tax`.
> - **Linking** (in `doc_links`, Ziel per `stable_key` / `repo:path`):
>   - *manual* (`--link`, gilt für jeden Chunk),
>   - *name_match*: nur Namen, die nach Code aussehen (`TaxCalculator.Breakdown`, `roundMoney()`, `tax_rate`) oder Typnamen in Backticks, exakt und ohne Properties/Felder. Prosa-Wörter wie „main“ oder „github“ erzeugten im ersten Test auf nest/eShop sonst nur Fehl-Links. Dokumente innerhalb eines Repos verlinken nur in dieses Repo (andere nur über qualifizierte Namen); mehrdeutige Pfade wie `src/app.module.ts` werden nur verlinkt, wenn sie eindeutig oder im selben Verzeichnisbaum sind.
>   - *route*: OpenAPI-Operationen auf die Server-Endpunkte aus Phase 3.
>   Die Semantik-Kandidaten aus 3.7 werden nicht gespeichert, sondern zur Abfragezeit über die semantische Doku-Suche gefunden.
> - **Suche:** FTS5-Tabelle `doc_chunks_fts` (LIKE-Fallback für andere DBs) plus Embeddings der Chunks im selben inhaltsadressierten Cache wie die Symbole. Context Packs enden mit einem Abschnitt „Documentation“ (verlinkte Chunks der Top-Treffer, dann Text-/Bedeutungstreffer, identische Chunks z. B. aus Spec v1/v2 nur einmal).
> - **Aktualisierung:** `wingman doc refresh` liest Dateien/URLs neu (Content-Hash) und baut alle Links neu auf. Kein automatisches Relinking bei jedem `wingman index`.
> - **Erster Praxistest:** 71 Dokumente aus nest, eShop und CleanArchitecture (READMEs, ADRs, zwei OpenAPI-Specs von eShop) in 19 s eingelesen; 74 Links, davon keiner mehr in ein fremdes Repo. Die Frage „Creating a catalog item through the HTTP API returns 400 …“ liefert im Context Pack die OpenAPI-Operation `POST /api/catalog/items`, verlinkt über `CatalogApi.CreateItem`.

---

## 4. Datenmodell

Ziel: eine **relationale Kernstruktur**, die in SQLite und PostgreSQL gleich aussieht. Graph-Abfragen laufen über Kantentabellen und rekursive CTEs, beides können beide DBs.

### 4.1 Übersicht

```
workspaces   1─n repositories
repositories 1─n index_runs
repositories 1─n files 1─n symbols ─┐ (parent_id: Verschachtelung)
                      │             │
                      └─1─n refs n──┘ (from_symbol → to_symbol, + target_name)
files 1─n imports
symbols n─n symbols   via symbol_edges (inherits, implements, overrides)
documents 1─n doc_chunks n─n (symbols|files) via doc_links
summaries / embeddings  (polymorph an symbol | file | doc_chunk)
```

### 4.2 Tabellen (SQL, vereinfacht)

```sql
-- Workspace = Gruppe zusammengehöriger Repos (z. B. Frontend + Backend + Shared Libs)
CREATE TABLE workspaces (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, created_at TIMESTAMP
);

-- Ein indexiertes Repository (mehrere pro DB)
CREATE TABLE repositories (
  id              INTEGER PRIMARY KEY,
  workspace_id    INTEGER REFERENCES workspaces(id),
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
  kind            TEXT NOT NULL,     -- class|struct|function|method|interface|enum|const|module|property|macro|...
  name            TEXT NOT NULL,
  qualified_name  TEXT NOT NULL,     -- pkg.module.Class.method
  signature       TEXT,
  type_name       TEXT,              -- deklarierter Werttyp (Feld/Property-Typ, Rückgabetyp ohne Task<>/Promise<>)
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
  to_symbol_id    INTEGER REFERENCES symbols(id) ON DELETE SET NULL,  -- NULL = nicht aufgelöst
  target_name     TEXT NOT NULL,     -- Name wie im Code (bleibt auch ohne Auflösung durchsuchbar)
  qualifier       TEXT,              -- Empfänger wie im Code: this, base, _calc, ns, _repo.UnitOfWork
  qualifier_type  TEXT,              -- deklarierter Typ des ersten Kettenglieds, falls bekannt
  kind            TEXT NOT NULL,     -- call|instantiate|type_ref|inherits|implements|http_call
  line INTEGER, col INTEGER,
  confidence      REAL NOT NULL DEFAULT 1.0,
  resolver        TEXT               -- local|import|heuristic|name|scip|lsp
);
CREATE INDEX ix_refs_to   ON refs(to_symbol_id);
CREATE INDEX ix_refs_from ON refs(from_symbol_id);

-- Weitere Orte eines Symbols: C/C++ Deklaration (Header) vs. Definition,
-- C# partial class über mehrere Dateien. Der Hauptort steht in symbols.
CREATE TABLE symbol_locations (
  symbol_id INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
  file_id   INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  role      TEXT NOT NULL,             -- declaration|definition|partial
  start_line INTEGER, end_line INTEGER
);

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
  id INTEGER PRIMARY KEY, workspace_id INTEGER REFERENCES workspaces(id),
  repo_id INTEGER REFERENCES repositories(id),   -- optional: Doku kann zu keinem einzelnen Repo gehören
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
  target_key TEXT NOT NULL,      -- symbol stable_key oder repo:path (keine IDs → teilbar, überlebt Re-Index)
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
  id INTEGER PRIMARY KEY, workspace_id INTEGER REFERENCES workspaces(id),
  target_type TEXT, target_key TEXT,  -- wie doc_links
  author TEXT, text TEXT, created_at TIMESTAMP
);
```

### 4.3 Multi-Repo

Mehrere Repos teilen sich eine DB und werden zu einem **Workspace** gruppiert. Suchen laufen per Default über den ganzen Workspace, mit `--repo` lässt sich einschränken. Jeder Treffer trägt den Repo-Namen (`backend:src/Billing/TaxCalculator.cs:40`).

Wichtig sind die **Kanten über Repo-Grenzen hinweg**, denn genau dort verliert ein Agent sonst die Spur:

| Art | Wie erkannt | Beispiel |
|-----|-------------|----------|
| **Paket-Abhängigkeit** | Paketname aus `package.json`/`.csproj`/`composer.json` des einen Repos ↔ Abhängigkeit im anderen. Imports aus diesem Paket werden in das Quell-Repo aufgelöst statt als „extern“ markiert | Frontend importiert `@firma/shared-models` → Symbole im Repo `shared` |
| **HTTP-API** | Server-Routen aus Attributen/Decorators (`[HttpGet("api/invoices/{id}")]`, `@Get()`), Client-Aufrufe aus `fetch`/`axios`/generierten Clients (URL-Muster normalisiert), optional OpenAPI-Spec als Brücke | `invoiceApi.get(id)` (TS) → `InvoicesController.Get(int id)` (C#) |
| **Gemeinsame Verträge** | DTO-/Typnamen, die in beiden Repos vorkommen (Name-Match, niedrige Konfidenz), bzw. aus OpenAPI generierte Typen | `InvoiceDto` (C#) ↔ `InvoiceDto` (TS) |
| **Messaging** (später) | Topic-/Queue-Namen als String-Literale in Publish/Subscribe-Aufrufen | `"invoice.created"` |

Dafür kommt eine zusätzliche Tabelle für API-Endpunkte dazu:

```sql
CREATE TABLE endpoints (
  id INTEGER PRIMARY KEY,
  symbol_id INTEGER REFERENCES symbols(id) ON DELETE CASCADE,
  role TEXT NOT NULL,          -- server|client
  http_method TEXT, route_pattern TEXT NOT NULL,   -- normalisiert: /api/invoices/{}
  confidence REAL DEFAULT 1.0
);
```

Server- und Client-Endpunkte mit gleichem `(http_method, route_pattern)` werden zu `refs` mit `kind = 'http_call'` verbunden. Damit funktionieren `get_callers` und die Graph-Expansion über die Grenze zwischen Frontend und Backend hinweg.

> *Umgesetzt in Phase 3:*
> - **Workspace-weiter Resolver** (`resolver.py`): Die Nachschlagetabellen umfassen alle Repos. Neu berechnet werden aber nur die Referenzen der Repos „im Scope“, also das geänderte Repo, Repos mit Referenzen in gelöschte Symbole und beim Erstindex eines Repos alle. Auflösung nur über den Namen (0,5) bleibt im eigenen Repo; repo-übergreifend zählt nur explizite Evidenz (Import, `using`).
> - **Pakete:** Alle `package.json` im Workspace (auch Monorepo-Pakete) bilden eine Registry. `import … from "@acme/shared"` bzw. `"@acme/shared/sub"` löst über `source`/`types`/`module`/`main` auf, wobei `dist/index.js` auf `src/index.ts` zurückgeführt wird, mit Fallback `src/index`. C#-`using`s finden Namespaces aus allen Repos.
> - **Server-Routen:** ASP.NET-Controller (`[Route("api/[controller]")]` + `[HttpGet("{id}")]`, `~/` absolut, konventionell `controller/action`), Minimal APIs (`MapGroup(…)…MapGet("/x", Handler)`, auch mit `.HasApiVersion(…)` dazwischen, Handler als Method-Group wird dem Handler zugeordnet), NestJS (`@Controller` + `@Get`) und Express (`app.get('/x', handler)`).
> - **Client-Aufrufe:** `fetch` (mit `method`), `axios.*`, Angular `this.http.get` sowie .NET `HttpClient.Get/Post/…Async` und `…AsJsonAsync`. Bekannte String-Konstanten und -Felder werden eingesetzt (`$"{remoteServiceBaseUrl}items/{id}"`). Ein unbekannter Anfang (Basis-URL) wird verworfen.
> - **Matching:** Routen werden normalisiert (Parameter, auch `{id:int?}`/`:id`/`${…}` → `{}`, Kleinschreibung, ohne Host und Query). Volle Übereinstimmung ergibt 0,9; Suffix bei unbekannter Basis-URL ergibt 0,6, dabei müssen literale Segmente exakt passen. Bei mehreren Kandidaten gewinnt die spezifischste Route (`items/by` vor `items/{id}`). Die `http_call`-Referenzen werden nach jeder Änderung workspace-weit neu aufgebaut. `wingman endpoints` bzw. das MCP-Tool `list_endpoints` zeigen Routen, Handler und Anzahl der Gegenstellen.
> - **Gemessen an dotnet/eShop:** 45 Server-Routen (Catalog, Ordering, Webhooks, Identity) und 63 Client-Aufrufe (WebApp, HybridApp, WebhookClient, Functional Tests), davon 62 einer Route zugeordnet. Bei nestjs/nest lösen 4.422 von 4.783 `@nestjs/*`-Imports in die Monorepo-Pakete auf; 543 Controller-Routen werden erkannt.
> - **Noch offen:** Messaging (Topics/Queues), gRPC sowie DTO-Abgleich über Namen.

### 4.4 Volltextsuche

- **SQLite**: `FTS5`-Virtual-Table über `symbols(name, qualified_name, signature, doc, summary)`, `doc_chunks(heading_path, text)` und optional Datei-Inhalt. Tokenizer `unicode61` mit `tokenchars '_'`, dazu eine eigene Vorverarbeitung, die `camelCase`/`snake_case` splittet, damit `InvoiceService` auch bei „invoice service“ trifft.
- **PostgreSQL**: `tsvector` + GIN-Index, gleiche Vorverarbeitung.

### 4.5 Vektorsuche

- **SQLite**: Extension [`sqlite-vec`](https://github.com/asg017/sqlite-vec). Fallback ohne Extension: Vektoren als BLOB laden und Brute-Force mit numpy. Bis ca. 100k Symbole ist das schnell genug.
- **PostgreSQL**: `pgvector` mit HNSW-Index.
- *Stand:* Bisher reicht Brute-Force mit numpy für beide Datenbanken (siehe 3.6), weil die Vektoren portabel als BLOB in `embedding_cache` liegen.

### 4.6 Austauschbare Datenbank

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

> *Umgesetzt in Phase 2* (`src/wingman/context.py`, ohne die semantische Suche aus Phase 4):
> - **Stacktrace-Frames** in C#- und JS/TS-Format, auch `pfad:zeile`. Windows- und `webpack://`-Pfade werden normalisiert. Pfade von fremden Maschinen werden über immer kürzere Suffixe gefunden (`D:\ci\work\Api\X.cs` → `Api/X.cs`). Die Fehlerstelle ist das innerste Symbol an der Zeile.
> - **Veraltete Zeilennummern:** Nennt der Frame einen anderen Methodennamen als das Symbol an der Zeile, stammt der Trace von einem älteren Build. Dann gewinnt der Name, und das Symbol an der Zeile bleibt nur ein schwacher Kandidat.
> - **Weitere Signale:** im Text genannte Bezeichner (Backticks, `CamelCase`, `snake_case`, `A.B`, `name()`) und genannte Dateien.
> - **Volltext:** FTS5 mit Porter-Stemming („cancels“ findet „Cancelled“) über den Prosa-Teil ohne Frame-Zeilen.
> - **Graph-Expansion:** 1 Hop zu Callern und Callees. Der Bonus ist gedeckelt, damit Nachbarn nie Symbole mit direkter Evidenz überholen.
> - **Diversifizierung:** höchstens 2 Member pro Klasse, Abwertung von Membern, die nur über den Klassennamen passen, Dämpfung gleichnamiger Kopien aus mehreren Services.
> - **Fusion:** gewichtete Summen statt RRF. Mit Embeddings (Phase 4) liefert die Suche Volltext- und semantische Kandidaten, beide auf 0..1 normiert und 50/50 gewichtet. Treffer sind mit „text“, „meaning“ oder „text+meaning“ markiert, im Context Pack als „similar meaning“.
> - **Bekannte Grenze:** Prosa ohne Bezeichner, die nah an *anderen* Klassennamen formuliert ist, findet die eigentliche Logik oft erst über die Graph-Expansion. Genau dafür ist die semantische Suche in Phase 4 gedacht.

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

*Umgesetzt:* `wingman context "<text>"` bzw. `--file bug.txt` (`--code`, `--budget`, `--json`). Jeder Eintrag zeigt zusätzlich, *warum* er passt („stack frame Order.cs:120“, „mentions `X`“, „text match“, „calls Y“). Außerdem enthält er Tests, die das Symbol über bis zu 2 Aufrufe erreichen, und Notizen zu Symbol, Datei oder Verzeichnis.

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

*Umgesetzt (Phase 1–2):* `init`, `repo add|list|remove`, `index [--full]`, `reindex <dateien>`, `status`, `search`, `context`, `symbol`, `callers`, `callees`, `tests`, `outline`, `note add|list|remove`, `mcp` (stdio), `hook post-edit|session-start` und `claude install|uninstall`. Alle Abfragen können `--json` ausgeben.

### 6.2 MCP-Server (Hauptintegration für Claude Code)

| Tool                   | Zweck                                                         |
|------------------------|---------------------------------------------------------------|
| `search_code`          | Hybrid-Suche mit Freitext, liefert ein Context Pack            |
| `find_relevant_for_task` | Nimmt einen Bug Report oder eine Aufgabe, liefert Context Pack plus vermutete Einstiegspunkte |
| `get_symbol`           | Definition, Signatur, Doku, Ort, optional Code                |
| `get_callers` / `get_callees` | Call-Graph mit Tiefe                                  |
| `get_file_outline`     | Symbolbaum einer Datei ohne Bodies                            |
| `find_tests_for`       | Tests, die ein Symbol (indirekt) aufrufen                     |
| `search_docs`          | Nur externe Doku durchsuchen *(Phase 6)*                      |
| `add_note`             | Agent hinterlegt Erkenntnisse für spätere Sessions            |
| `reindex`              | Nach Änderungen gezielt (Dateien) oder komplett inkrementell neu indexieren |

*Umgesetzt mit dem offiziellen Python-MCP-SDK (2.x)* in `src/wingman/mcp_server.py`, inklusive Server-Instructions und Tool-Annotations (read-only). Die Ausgaben sind kompakter Text mit `repo:pfad:start-ende`. Unbekannte oder mehrdeutige Namen liefern Hinweise statt Fehler.

Einbindung: `wingman claude install [projekt]` (siehe 6.3), manuell `claude mcp add wingman -- wingman mcp`.

### 6.3 Claude-Code-Integration darüber hinaus

- **Hooks**: Ein `PostToolUse`-Hook auf `Edit`/`Write` ruft `wingman reindex <file>` auf. Ein `SessionStart`-Hook führt `wingman index --incremental` aus.
  - *Umgesetzt:*
    - `wingman hook post-edit` liest den Hook-Payload von stdin und indexiert nur die bearbeitete Datei: ca. 20 ms plus Resolver-Lauf, der bei unveränderten Dateien entfällt.
    - `wingman hook session-start` bringt alle Repos auf Stand und gibt dem Agenten einen Satz Kontext („Index bereit, nutze find_relevant_for_task …“).
    - Hooks scheitern nie laut: Exit-Code 0, Fehler nur auf stderr.
  - `wingman claude install [projekt]` richtet MCP-Server, Hooks und optional (`--claude-md`) einen CLAUDE.md-Abschnitt ein. Das ist idempotent, fremde Hooks bleiben erhalten, und `uninstall` räumt rückstandslos ab. Es gibt zwei Modi:
    - **Standard (lokal):** absolute Pfade, `claude mcp add --scope local` und `.claude/settings.local.json`.
    - **`--shared`:** portabel und zum Committen gedacht, mit `.mcp.json` und `.claude/settings.json`. Dafür muss `wingman` im PATH sein, und der Workspace wird durch Aufwärtssuche vom Projekt gefunden.
- **Skill / CLAUDE.md-Snippet**: kurze Anleitung „Nutze zuerst `search_code`/`find_relevant_for_task`, bevor du großflächig grep-st“.
- **Python-API** für eigene Skripte und andere Agent-Frameworks.

---

## 7. Projektstruktur (Stand Phase 1, später geplante Module eingerückt markiert)

```
agent-wingman/
├── pyproject.toml
├── README.md
├── CONCEPT.md
├── src/wingman/
│   ├── cli.py
│   ├── config.py               # .wingman/config.toml + Env
│   ├── scanner.py              # git ls-files / Walk, Ignore-Regeln, Test-/Generated-Erkennung
│   ├── extract/                # tree-sitter-Visitor je Sprache: typescript.py, csharp.py
│   ├── indexer.py              # inkrementell: Hash-Vergleich, Speichern, FTS
│   ├── resolver.py             # Imports, Typen, Aufrufe, Vererbung, overrides-Kanten
│   ├── query.py                # search, symbol, callers, callees, tests, outline
│   ├── context.py              # Bug-Report/Aufgabe → Signale → Ranking → Context Pack
│   ├── render.py               # Textausgabe für CLI und MCP
│   ├── notes.py                # Notizen an Symbolen/Dateien/Verzeichnissen
│   ├── mcp_server.py           # MCP-Server (stdio)
│   ├── hooks.py                # Claude-Code-Hooks (post-edit, session-start)
│   ├── claude_setup.py         # wingman claude install/uninstall
│   ├── (später) enrich/        # LLM-Summaries, Provider-Interface
│   ├── embeddings.py           # Embedding-Texte, Provider, Vektor-Cache, semantische Suche
│   ├── endpoints.py            # HTTP-Routen normalisieren, Client ↔ Server matchen
│   ├── (später) docs/          # PDF/URL/MD-Ingest, Chunker, Linker
│   ├── storage/
│   │   ├── schema.py           # SQLAlchemy Core Tabellen
│   │   ├── migrations/         # Alembic
│   │   ├── store.py            # Workspaces & Repositories
│   │   └── search.py           # SearchBackend: SQLite FTS5, LIKE-Fallback (später tsvector/pgvector)
│   ├── (später) resolve/scip   # SCIP/LSP-Adapter
│   └── workspace.py            # Config + DB für CLI, MCP und Hooks
└── tests/
    └── fixtures/               # Mini-Repos je Sprache mit erwarteten Indizes
```

Lokaler Zustand pro Projekt in `.wingman/` (DB, Cache, Config). Kommt in `.gitignore`, optional teilbar.

---

## 8. Roadmap

Der Prototyp ist auf **einen Entwickler mit lokaler SQLite-DB** ausgelegt. Das Datenmodell wird aber von Anfang an so gebaut, dass Teilen später ohne Umbau möglich ist (siehe Abschnitt 10).

| Phase | Inhalt | Ergebnis |
|------|--------|----------|
| **0 – Skeleton** ✅ | `pyproject`, CLI-Gerüst, Config, SQLite-Schema + Alembic, Workspace/Repo-Verwaltung | `wingman init`, `wingman repo add` laufen |
| **1 – Strukturindex** ✅ | Scanner, tree-sitter für **TypeScript + C#**, Symbole, Imports, Refs, lokaler/Import-Resolver (inkl. `tsconfig`-Pfade, C#-Namespaces), FTS5 | `wingman index`, `search`, `symbol`, `callers`, `callees`, `outline` |
| **2 – MCP** ✅ | MCP-Server mit Kern-Tools, Context Pack, inkrementelles Indexing, Hooks | In Claude Code nutzbar |
| **3 – Multi-Repo** ✅ | Paket-Abhängigkeiten zwischen Repos, HTTP-Endpunkt-Matching (ASP.NET ↔ fetch/axios) | Agent folgt Aufrufen vom Frontend ins Backend |
| **4 – Semantik** ✅ (vorgezogen) | Embeddings (lokal, fastembed), inhaltsadressierter Vektor-Cache, Hybrid-Suche (gewichtete Fusion statt RRF) + Graph-Expansion. Gemessen mit echtem Modell (Abschnitt 9); `sqlite-vec` erst bei Bedarf | Bug-Report → relevante Stellen |
| **5 – Summaries** ✅ (ohne echten API-Lauf) | LLM-Enricher bottom-up (Anthropic, kleines Modell, Opt-in), Cache, Budget, Datei-/Modul-Summaries | Undokumentierter Code wird durchsuchbar |
| **6 – Externe Doku** ✅ | PDF/URL/MD/OpenAPI-Ingest, Chunking, Auto-Linking | Spezifikationen hängen am Code |
| **7 – Breite** | JavaScript, dann Java, Python, PHP, dann **C/C++** (inkl. `compile_commands.json`, Header/Source-Zuordnung); SCIP/Roslyn/clang-Adapter; Notes | Alle Zielsprachen abgedeckt |
| **8 – Teilen** | Export/Import-Format, CI-Build, PostgreSQL-Backend (siehe Abschnitt 10) | Team nutzt einen gemeinsamen Index |
| **9 – Evaluation** | Benchmark: echte Issues/Commits, Recall@k der geänderten Dateien (läuft ab Phase 2 mit, wird hier ausgebaut) | Messbare Qualität, Tuning |

### Todo – für später vorgemerkt

- [ ] **Mehrsprachige Bug-Reports:** Das Standardmodell `bge-small-en-v1.5` findet deutsche Anfragen praktisch nicht (Messung in Abschnitt 9). Idee: ein zweites, mehrsprachiges Modell (z. B. `paraphrase-multilingual-MiniLM-L12-v2`) parallel einbetten und nur für nicht-englische Anfragen nutzen. Der Vektor-Cache ist schon nach Modell getrennt. Kosten: doppelte Einbettzeit. Erst angehen, wenn nicht-englische Reports gebraucht werden.
- [x] **Rauschen bei Stacktraces:** Semantische Treffer aus Repos, die in keinem Frame vorkommen (z. B. `InvalidDecoratorItemException` aus nest bei einem eShop-Stacktrace), könnten abgewertet werden. *Erledigt:* Text- und Bedeutungstreffer aus Repos ohne Stack-Frame zählen nur noch 40 %; im eShop-Beispiel ist der nest-Treffer aus den Top 8 verschwunden.
- [ ] **Summaries mit echter API messen:** Qualität und tatsächliche Kosten mit `claude-haiku-5-5` auf eShop prüfen (braucht einen API-Key).
- [ ] **Datei- und Modul-Summaries** aus den Symbol-Summaries aggregieren (Abschnitt 3.5).
- [ ] **Batches-API für große Erstläufe:** halber Preis, dafür asynchron (Minuten bis Stunden).

---

## 9. Qualität messen

Die gute Nachricht: Git-Historien liefern Ground Truth gratis. Für jeden Bugfix-Commit oder jede Issue-PR-Kombination gilt: Issue-Text als Query, die im Fix geänderten Dateien und Symbole als erwartete Treffer. Kennzahlen:

- **Recall@k** (Datei- und Symbolebene), **MRR**
- **Token-Effizienz**: Wie viele Tokens braucht der Agent bis zur ersten richtigen Datei, mit und ohne wingman?
- Indexing-Dauer und DB-Größe pro 100k LOC

**Erste Messung (Phase 1, ohne Embeddings und LLM-Summaries):**

| Repo | Dateien | Symbole + Referenzen | Erstindex | Re-Index ohne Änderung |
|------|---------|----------------------|-----------|------------------------|
| jasontaylordev/CleanArchitecture (C#/TS) | 134 | 2.145 Refs | 0,6 s | < 0,2 s |
| dotnet/eShop (C#) | 553 | 16k Refs | 2,5 s | 0,2 s |
| nestjs/nest (TS) | 2.157 | 83k Refs | 12 s | 1,5 s |

Zusammen sind das 28 MB SQLite. Eine Suche dauert unter 1 s. Vererbung, Typreferenzen und Instanziierungen werden aufgelöst, soweit das Ziel im Repo liegt. Der größte Teil der unaufgelösten Referenzen sind Aufrufe in Framework- und Bibliothekscode (BCL, ASP.NET, npm). Das ist erwartet und wird mit Multi-Repo/Paketauflösung (Phase 3) und optionalen SCIP-Indizes kleiner.

**Messung mit echter semantischer Suche (Phase 4, `bge-small-en-v1.5`, 4 CPU-Kerne):**

- **Kosten:**
  - Modell-Download ca. 70 MB, einmalig ca. 5 s.
  - Laden des Modells ca. 1,3 s pro CLI-Aufruf; der MCP-Server lädt es nur einmal.
  - Eine Anfrage einbetten: ca. 7 ms.
- **Erst-Embedding aller drei Repos:** 14.930 Symbole in 15 min. Unveränderter Code wird danach nie wieder eingebettet.
- **Prosa-Bug-Report** („buyer cancels an order that was already paid …“): `Order.SetCancelledStatus` steht mit Semantik auf Platz 1. Ohne Semantik ist es nicht unter den Top 8, dort dominieren die Integration-Event-Klassen aus dem Bezeichner im Text.
- **Stacktrace:** Die Plätze 1 und 2 sind mit und ohne Semantik gleich (beide Frames). Mit Semantik rutscht auf Platz 3 eine ähnlich klingende Exception-Klasse aus einem anderen Repo (`InvalidDecoratorItemException` in nest), ein bekanntes Rauschen.
- **Deutscher Bug-Report:** Ohne passende Treffer, siehe „Sprache“ in Abschnitt 3.6.

---

## 10. Teilen des Index im Team – Brainstorming

Für den Prototyp reicht ein lokaler Index pro Entwickler. Die Kernfrage für später: **Was ist teuer zu erzeugen, und was ist billig?**

| Bestandteil | Kosten | Folgerung |
|-------------|--------|-----------|
| Strukturindex (Symbole, Refs) | billig, Sekunden bis wenige Minuten, deterministisch | muss nicht geteilt werden, lokal neu bauen ist okay |
| Embeddings | mittel (CPU/GPU-Zeit), deterministisch je Modell | Teilen lohnt sich bei großen Codebasen |
| **LLM-Summaries** | **teuer** (Geld + Zeit), nicht deterministisch | **unbedingt teilen**, sonst bezahlt jeder Dev dieselben Summaries |
| Externe Doku + Links, Notes | manuell kuratiert, wertvoll | **unbedingt teilen**, das ist Teamwissen |

Daraus folgt das Leitprinzip: **Alles Teure wird inhaltsadressiert gespeichert**, d. h. der Schlüssel ist ein Hash des Inhalts (`body_hash + model + prompt_version`) und keine DB-ID oder Commit-ID. Dann gibt es beim Zusammenführen keine Konflikte, denn derselbe Code hat denselben Schlüssel, egal auf welchem Branch und bei welchem Dev. Gleiche Schlüssel mit unterschiedlichem Wert (zwei LLM-Läufe) sind harmlos: Man nimmt einen davon.

### Option A – Über git: Export als Textdateien im Repo

`wingman export` schreibt den teuren, geteilten Teil als **deterministisch sortierte JSONL-Dateien** in einen Ordner im Repo:

```
.wingman/shared/
  summaries/ab/abcd1234….jsonl   # Shards nach Hash-Präfix, klein & merge-freundlich
  docs/documents.jsonl           # externe Doku-Metadaten (URLs, Titel), PDFs per Git LFS oder nur Referenz
  docs/links.jsonl               # Doku ↔ Code (über stable_key, nicht über IDs)
  notes.jsonl
  manifest.json                  # Format-Version, Modelle, Prompt-Versionen
```

- Nach `git pull` liest `wingman index` die Dateien ein, baut den Strukturindex lokal und übernimmt dabei vorhandene Summaries per Hash, statt sie neu zu erzeugen.
- **Vorteile:** Keine Infrastruktur nötig, versioniert mit dem Code, Review per PR möglich, funktioniert offline und auf Branches.
- **Nachteile:** Rauschen in der Git-Historie. Lösungen dafür: eigener Commit-Typ, Pfad in `.gitattributes` mit `linguist-generated` und `merge=union`, oder (siehe unten) ein separater Branch.
- Embeddings eher **nicht** ins Repo (Binärdaten, groß), sondern lokal neu rechnen oder Variante C nutzen.

**Variante A2 – eigener Branch:** Die gleichen Dateien landen nicht im Code-Branch, sondern auf einem Orphan-Branch `wingman-index` (oder in einem eigenen Repo). `wingman sync` holt bzw. pusht ihn. Die Code-Historie bleibt sauber, Teilen läuft trotzdem über dieselbe git-Infrastruktur und dieselben Rechte.

### Option B – Gemeinsame Datenbank (PostgreSQL + pgvector)

- Ein zentraler Server, alle Devs und die CI schreiben bzw. lesen.
- **Das Branch-Problem:** Jeder Dev hat einen anderen Stand. Lösung über ein **Overlay-Modell**:
  - Der zentrale Index bildet `main` (bzw. den Default-Branch) ab und wird von der CI bei jedem Merge aktualisiert.
  - Jeder Dev hat lokal ein kleines SQLite-**Overlay** nur für die Dateien, die sich gegenüber `main` unterscheiden (`git diff --name-only origin/main`).
  - Eine Abfrage fragt beide und lässt bei denselben Dateien das Overlay gewinnen.
- **Vorteile:** Immer aktuell, kein Export-Schritt, gut für große Teams und viele Repos, Embedding-Suche zentral schnell.
- **Nachteile:** Server-Betrieb, Zugangsdaten, nicht offline, Code-Inhalte (Summaries, Snippets) liegen in einer weiteren Datenbank, was Sicherheits- und Compliance-Fragen aufwirft.

### Option C – CI-Artefakt

- Die CI (z. B. GitHub Actions) baut bei jedem Push auf `main` den vollständigen Index inkl. Embeddings und Summaries und legt ihn als Artefakt oder Release-Asset ab (z. B. `index-<commit>.db.zst`).
- `wingman pull-index` lädt den Index zum nächstgelegenen Vorfahren-Commit von `HEAD` und indexiert nur die Differenz lokal nach.
- **Vorteile:** Keine eigene Infrastruktur, Devs müssen nie selbst LLM-Kosten erzeugen, schneller Start für neue Teammitglieder.
- **Nachteile:** Download-Größe, und lokale Ergänzungen (Notes, Doku-Links) fließen nicht automatisch zurück. Für diese braucht es zusätzlich Option A.

### Erste Einschätzung

Die Optionen schließen sich nicht aus, sondern lassen sich stufenweise einführen:

1. **Prototyp:** lokal, aber schon inhaltsadressiert und mit `stable_key` statt IDs bei Verknüpfungen. Das kostet jetzt fast nichts und macht alles Weitere möglich.
2. **Erster Teamschritt:** **Option A2** (Orphan-Branch). Damit sind Summaries, Doku-Links und Notes geteilt, ohne neue Infrastruktur.
3. **Bei Bedarf:** Option C für schnelle Bootstraps großer Repos, und **Option B** für Teams, die ohnehin eine zentrale DB betreiben wollen. Das Backend-Interface aus 4.6 ist darauf schon vorbereitet.

Offene Punkte zum Weiterdiskutieren:
- Sollen PDFs selbst geteilt werden (Git LFS) oder nur Referenzen darauf (Pfad im Netzlaufwerk, URL)?
- Wer darf Notes schreiben? Nur Menschen, oder auch der Agent? Braucht es einen Review-Mechanismus?
- Datenschutz: Dürfen generierte Summaries in eine zentrale DB, wenn der Code selbst sensibel ist?

---

## 11. Entscheidungen

| # | Thema | Entscheidung |
|---|-------|--------------|
| 1 | Embedding-Modell | **Lokal als Default** (`sentence-transformers`, code-fähiges Modell), API-Provider optional per Config |
| 2 | LLM-Summaries | **Anthropic als Default**, kleines und günstiges Modell, **Opt-in** per Config, mit Budget-Limit. Andere Provider (OpenAI-kompatibel, Ollama) über das Provider-Interface |
| 3 | Multi-Repo | **Ja, von Anfang an**: Workspaces mit mehreren Repos in einer DB, Kanten über Repo-Grenzen (Pakete, HTTP-Endpunkte), siehe 4.3 |
| 4 | Sprachen | **TypeScript + C#** im Prototyp, danach **JavaScript**, dann **Java, Python, PHP**, danach **C und C++**. Weitere Sprachen (Go, Rust, Kotlin …) bleiben über weitere tree-sitter-Extractors jederzeit nachrüstbar |
| 5 | Teilen | **Prototyp: ein Dev, lokale SQLite.** Datenmodell wird teilbar ausgelegt (inhaltsadressiert, `stable_key`). Teamlösung siehe Brainstorming in Abschnitt 10, Entscheidung später |
