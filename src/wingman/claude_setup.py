"""Wires agent-wingman into Claude Code for a project: MCP server, hooks, CLAUDE.md.

Two modes:
  local  (default) – machine-specific absolute paths; MCP server registered with
                     ``claude mcp add --scope local``, hooks in .claude/settings.local.json
  shared           – portable commands (``wingman`` on PATH, workspace found by walking
                     up from the project); .mcp.json and .claude/settings.json, meant to
                     be committed
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

SERVER_NAME = "wingman"
# identifies our hook entries in settings files (exec form and older shell-form entries)
_OUR_HOOK = re.compile(r"wingman\b.* hook (post-edit|session-start)$")
CLAUDE_MD_START = "<!-- wingman:start -->"
CLAUDE_MD_END = "<!-- wingman:end -->"
CLAUDE_MD_SNIPPET = f"""{CLAUDE_MD_START}
## Code index (agent-wingman)

This workspace is indexed by agent-wingman (MCP server `wingman`). Before searching
files manually:
- For bug reports, stack traces and tasks call `find_relevant_for_task` with the full text.
- Use `search_code`, `get_symbol`, `get_callers`, `get_callees` and `find_tests_for` to
  navigate; locations are `repo:path:start-end`.
- Record non-obvious findings with `add_note` so later sessions know them.
{CLAUDE_MD_END}
"""


@dataclass
class SetupResult:
    changed: list[str] = field(default_factory=list)
    instructions: list[str] = field(default_factory=list)


def base_command(home: Path | None, shared: bool) -> list[str]:
    if shared:
        return ["wingman"]
    command = [sys.executable, "-m", "wingman"]
    if home is not None:
        command += ["--home", str(home)]
    return command


def hook_settings(base: list[str]) -> dict:
    def entry(event: str) -> dict:
        # Exec form (command + args): Claude Code starts the program directly, without
        # a shell, so paths with spaces or backslashes need no quoting. In shell form
        # Windows runs hooks through Git Bash or PowerShell, which quote differently.
        return {
            "type": "command",
            "command": base[0],
            "args": [*base[1:], "hook", event],
            "timeout": 120 if event == "session-start" else 60,
        }

    return {
        "PostToolUse": [
            {"matcher": "Edit|MultiEdit|Write|NotebookEdit", "hooks": [entry("post-edit")]}
        ],
        "SessionStart": [{"hooks": [entry("session-start")]}],
    }


def _hook_text(hook: dict) -> str:
    args = hook.get("args")
    command = str(hook.get("command", ""))
    return shlex.join([command, *map(str, args)]) if isinstance(args, list) else command


def _is_ours(group: dict) -> bool:
    return any(_OUR_HOOK.search(_hook_text(h)) for h in group.get("hooks", []))


def merge_hooks(settings: dict, ours: dict) -> dict:
    """Replace previous wingman hook entries, keep everything else."""
    hooks = settings.setdefault("hooks", {})
    for event, groups in ours.items():
        existing = [g for g in hooks.get(event, []) if not _is_ours(g)]
        hooks[event] = existing + groups
    return settings


def remove_hooks(settings: dict) -> dict:
    hooks = settings.get("hooks", {})
    for event in list(hooks):
        hooks[event] = [g for g in hooks[event] if not _is_ours(g)]
        if not hooks[event]:
            del hooks[event]
    if not hooks:
        settings.pop("hooks", None)
    return settings


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def install(
    project: Path,
    home: Path | None,
    *,
    shared: bool = False,
    claude_md: bool = False,
    register_mcp: bool = True,
) -> SetupResult:
    result = SetupResult()
    base = base_command(home, shared)

    settings_path = project / ".claude" / ("settings.json" if shared else "settings.local.json")
    _write_json(settings_path, merge_hooks(_read_json(settings_path), hook_settings(base)))
    result.changed.append(str(settings_path))

    if register_mcp:
        if shared:
            mcp_path = project / ".mcp.json"
            data = _read_json(mcp_path)
            data.setdefault("mcpServers", {})[SERVER_NAME] = {
                "command": base[0],
                "args": [*base[1:], "mcp"],
            }
            _write_json(mcp_path, data)
            result.changed.append(str(mcp_path))
        else:
            add = ["claude", "mcp", "add", "--scope", "local", SERVER_NAME, "--", *base, "mcp"]
            if shutil.which("claude"):
                subprocess.run(
                    ["claude", "mcp", "remove", "--scope", "local", SERVER_NAME],
                    cwd=project,
                    capture_output=True,
                    check=False,
                )
                done = subprocess.run(add, cwd=project, capture_output=True, text=True)
                if done.returncode == 0:
                    result.changed.append("Claude Code MCP config (local scope)")
                else:
                    result.instructions.append(
                        f"Registering the MCP server failed ({done.stderr.strip()}); run:\n"
                        f"  cd {shlex.quote(str(project))} && {shlex.join(add)}"
                    )
            else:
                result.instructions.append(
                    "Claude Code CLI not found. Register the MCP server with:\n"
                    f"  cd {shlex.quote(str(project))} && {shlex.join(add)}"
                )

    if claude_md:
        path = project / "CLAUDE.md"
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        if CLAUDE_MD_START in text:
            start = text.index(CLAUDE_MD_START)
            end = text.index(CLAUDE_MD_END, start) + len(CLAUDE_MD_END) + 1
            text = text[:start] + CLAUDE_MD_SNIPPET + text[end:]
        else:
            text = (text.rstrip() + "\n\n" if text.strip() else "") + CLAUDE_MD_SNIPPET
        path.write_text(text, encoding="utf-8")
        result.changed.append(str(path))
    return result


def uninstall(project: Path, *, shared: bool = False) -> SetupResult:
    result = SetupResult()
    settings_path = project / ".claude" / ("settings.json" if shared else "settings.local.json")
    if settings_path.is_file():
        remaining = remove_hooks(_read_json(settings_path))
        if remaining:
            _write_json(settings_path, remaining)
        else:
            settings_path.unlink()
        result.changed.append(str(settings_path))
    if shared:
        mcp_path = project / ".mcp.json"
        data = _read_json(mcp_path)
        if data.get("mcpServers", {}).pop(SERVER_NAME, None) is not None:
            if data["mcpServers"] or len(data) > 1:
                _write_json(mcp_path, data)
            else:
                mcp_path.unlink()
            result.changed.append(str(mcp_path))
    elif shutil.which("claude"):
        subprocess.run(
            ["claude", "mcp", "remove", "--scope", "local", SERVER_NAME],
            cwd=project,
            capture_output=True,
            check=False,
        )
        result.changed.append("Claude Code MCP config (local scope)")
    path = project / "CLAUDE.md"
    if path.is_file():
        text = path.read_text(encoding="utf-8")
        if CLAUDE_MD_START in text:
            start = text.index(CLAUDE_MD_START)
            end = text.index(CLAUDE_MD_END, start) + len(CLAUDE_MD_END)
            remaining = (text[:start].rstrip() + "\n\n" + text[end:].lstrip("\n")).strip()
            if remaining:
                path.write_text(remaining + "\n", encoding="utf-8")
            else:
                path.unlink()
            result.changed.append(str(path))
    return result
