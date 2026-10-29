"""Measuring retrieval quality on the git history (CONCEPT.md section 9).

Every bug-fix commit is a free test case: its message is the query (like a bug
report), the files it changed are the expected answer. ``evaluate`` runs the
context pack for each message and records at which rank the first changed file
(and, where possible, the first changed symbol) appears.

Changed symbols are only known for files that did not change after the commit:
then the line numbers of the diff still match the indexed version.

Caveat: the index is built from the current code, which already contains the fix,
and commit messages often name the changed code. Absolute numbers are therefore
optimistic; the harness is meant for comparing settings and versions.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import Connection, select

from wingman import context
from wingman.extract import spec_for
from wingman.scanner import is_generated, is_test_path
from wingman.storage.schema import files, repositories, symbols

DEFAULT_PATTERN = r"\b(fix(e[sd])?|bug|issue|crash|error|wrong|broken|regression)\b"
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)
MIN_WORDS = 3
_NOISE = re.compile(
    r"#\d+|\b(fix(e[sd])?|bugs?|issues?|minor|small|typo|wip|chore|feat)\b|[^\w\s]", re.I
)
SYMBOL_KINDS = ("function", "method", "constructor", "component", "property", "route")


@dataclass
class Case:
    sha: str
    subject: str
    text: str
    files: list[str]
    lines: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    """Changed line ranges (new version) of files unchanged since the commit."""


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout


def history_cases(
    root: Path,
    *,
    limit: int = 30,
    pattern: str = DEFAULT_PATTERN,
    max_files: int = 8,
    scan_commits: int = 2000,
) -> list[Case]:
    """Recent non-merge commits whose message matches ``pattern`` and that changed
    1..``max_files`` indexed source files (tests and generated files excluded)."""
    log = _git(
        root,
        "log",
        "--no-merges",
        f"-n{scan_commits}",
        "-i",
        "-E",
        f"--grep={pattern}",
        "--format=%H%x00%s%x00%b%x1e",
    )
    cases: list[Case] = []
    for record in log.split("\x1e"):
        parts = record.strip("\n").split("\x00")
        if len(parts) < 3 or not parts[0]:
            continue
        sha, subject, body = parts[0].strip(), parts[1].strip(), parts[2].strip()
        changed = _git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", sha).split()
        sources = [
            p
            for p in changed
            if spec_for(p) is not None and not is_test_path(p) and not is_generated(p, b"")
        ]
        sources = [p for p in sources if (root / p).is_file()]  # still exists
        if not sources or len(sources) > max_files:
            continue
        text = f"{subject}\n\n{body}".strip()
        if len(_NOISE.sub(" ", text).split()) < MIN_WORDS:
            continue  # "Fix bug", "Fixes #1082": nothing to search for
        cases.append(Case(sha, subject, text, sources, _unchanged_hunks(root, sha, sources)))
        if len(cases) >= limit:
            break
    return cases


def _unchanged_hunks(root: Path, sha: str, paths: list[str]) -> dict[str, list[tuple[int, int]]]:
    unchanged = [
        p
        for p in paths
        if subprocess.run(
            ["git", "-C", str(root), "diff", "--quiet", sha, "HEAD", "--", p], check=False
        ).returncode
        == 0
    ]
    result: dict[str, list[tuple[int, int]]] = {}
    for path in unchanged:
        diff = _git(root, "show", "-U0", "--format=", sha, "--", path)
        ranges = []
        for match in _HUNK.finditer(diff):
            start = int(match.group(1))
            count = int(match.group(2)) if match.group(2) is not None else 1
            if count > 0:
                ranges.append((start, start + count - 1))
            else:  # pure deletion: the line after it is the closest anchor
                ranges.append((start, start))
        if ranges:
            result[path] = ranges
    return result


@dataclass
class CaseResult:
    case: Case
    file_rank: int | None
    """1-based rank of the first entry in a changed file, None if not in the pack."""
    symbol_rank: int | None
    expected_symbols: list[str]
    top: list[str]


@dataclass
class Report:
    results: list[CaseResult]
    k: int

    def recall(self, at: int, *, symbols: bool = False) -> float | None:
        ranks = self._ranks(symbols)
        if not ranks:
            return None
        return sum(1 for r in ranks if r is not None and r <= at) / len(ranks)

    def mrr(self, *, symbols: bool = False) -> float | None:
        ranks = self._ranks(symbols)
        if not ranks:
            return None
        return sum(1 / r for r in ranks if r is not None) / len(ranks)

    def _ranks(self, symbols: bool) -> list[int | None]:
        if symbols:
            return [r.symbol_rank for r in self.results if r.expected_symbols]
        return [r.file_rank for r in self.results]

    @property
    def symbol_cases(self) -> int:
        return sum(1 for r in self.results if r.expected_symbols)

    def to_dict(self) -> dict:
        def metrics(symbols: bool) -> dict:
            return {
                **{f"recall@{n}": self.recall(n, symbols=symbols) for n in (1, 3, 5, self.k)},
                "mrr": self.mrr(symbols=symbols),
            }

        return {
            "cases": len(self.results),
            "k": self.k,
            "files": metrics(False),
            "symbols": {"cases": self.symbol_cases, **metrics(True)},
            "results": [
                {
                    "sha": r.case.sha,
                    "subject": r.case.subject,
                    "changed_files": r.case.files,
                    "file_rank": r.file_rank,
                    "expected_symbols": r.expected_symbols,
                    "symbol_rank": r.symbol_rank,
                    "top": r.top,
                }
                for r in self.results
            ],
        }


def _expected_symbols(conn: Connection, repo_id: int, case: Case) -> dict[int, str]:
    """Innermost symbols (functions, methods, ...) touched by the diff."""
    expected: dict[int, str] = {}
    for path, ranges in case.lines.items():
        rows = conn.execute(
            select(
                symbols.c.id,
                symbols.c.qualified_name,
                symbols.c.start_line,
                symbols.c.end_line,
            )
            .join(files, files.c.id == symbols.c.file_id)
            .where(
                files.c.repo_id == repo_id,
                files.c.path == path,
                symbols.c.kind.in_(SYMBOL_KINDS),
            )
        ).all()
        for start, end in ranges:
            touching = [r for r in rows if r.start_line <= end and r.end_line >= start]
            if touching:
                inner = min(touching, key=lambda r: r.end_line - r.start_line)
                expected[inner.id] = inner.qualified_name
    return expected


def evaluate(
    conn: Connection,
    workspace_id: int,
    repo: str,
    cases: list[Case],
    *,
    k: int = 10,
    semantic: Callable[[str], list[tuple[int, float]] | None] | None = None,
    progress: Callable[[str], None] | None = None,
) -> Report:
    repo_id = conn.execute(
        select(repositories.c.id).where(
            repositories.c.workspace_id == workspace_id, repositories.c.name == repo
        )
    ).scalar_one()
    results = []
    for number, case in enumerate(cases, 1):
        pack = context.find_relevant(
            conn, workspace_id, case.text, limit=k, semantic=semantic, max_docs=0
        )
        changed = set(case.files)
        file_rank = next(
            (
                i
                for i, e in enumerate(pack.entries, 1)
                if e.hit.repo == repo and e.hit.path in changed
            ),
            None,
        )
        expected = _expected_symbols(conn, repo_id, case)
        symbol_rank = next((i for i, e in enumerate(pack.entries, 1) if e.hit.id in expected), None)
        results.append(
            CaseResult(
                case,
                file_rank,
                symbol_rank,
                sorted(expected.values()),
                [f"{e.hit.qualified_name} ({e.hit.path})" for e in pack.entries],
            )
        )
        if progress and number % 10 == 0:
            progress(f"{number}/{len(cases)} cases")
    return Report(results, k)
