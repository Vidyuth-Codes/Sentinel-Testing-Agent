"""Claude Code engine — the reasoning brain, on the team's subscription.

Drives the Claude Code CLI in headless `print` mode via a subprocess. This avoids the
Agent SDK's bidirectional control protocol (whose `initialize` handshake hangs/contends on
this Windows setup) and — because `subprocess.run(timeout=...)` terminates the child — it
never leaves orphaned `claude` processes behind.

Read-only tools only, so it inspects the addon but never edits it. Auth flows through the
CLI's login: with a Claude subscription signed in and no ANTHROPIC_API_KEY present, runs are
billed to the subscription (not metered API).

Prerequisite (one-time):
    npm install -g @anthropic-ai/claude-code
    claude         # sign in with the Claude subscription
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from sentinel.paths import repo_root

_READONLY_TOOLS = "Read,Grep,Glob"
_MAX_SYSTEM_PROMPT = 24000  # keep the whole command line under the Windows ~32k limit


class EngineUnavailable(Exception):
    """The Claude Code CLI isn't installed / resolvable, or a run failed to start."""


@dataclass
class EngineResult:
    text: str
    session_id: str | None = None
    cost_usd: float | None = None
    is_error: bool = False
    engine: str = "claude-code"


def _neutral_workspace() -> str:
    """cwd for the CLI = the (non-git) Sentinel repo. Pointing cwd at the addon (a git
    repo) makes the CLI's git-aware startup very slow; we keep cwd here and grant the
    addon via --add-dir / absolute-path reads instead."""
    return str(repo_root())


def _find_cli() -> str | None:
    """Prefer the native bin\\claude.exe (clean subprocess, no .cmd arg limits)."""
    override = os.environ.get("SENTINEL_CLAUDE_PATH")
    if override and Path(override).exists():
        return override
    appdata = os.environ.get("APPDATA")
    if appdata:
        native = Path(appdata) / "npm" / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        if native.exists():
            return str(native)
    for name in ("claude.exe", "claude", "claude.cmd"):
        found = shutil.which(name)
        if found:
            return found
    if appdata:
        cmd = Path(appdata) / "npm" / "claude.cmd"
        if cmd.exists():
            return str(cmd)
    return None


class ClaudeCodeEngine:
    def __init__(self) -> None:
        self.cli_path = _find_cli()

    def available(self) -> bool:
        return self.cli_path is not None

    def run_sync(
        self, prompt: str, *, code_dir: str | None = None, extra_dirs: list[str] | None = None,
        system_prompt: str = "", resume: str | None = None, max_turns: int = 40, timeout: int = 600,
    ) -> EngineResult:
        if not self.available():
            raise EngineUnavailable("`claude` CLI not found (npm install -g @anthropic-ai/claude-code)")

        code_dir = code_dir if (code_dir and Path(code_dir).is_dir()) else None  # no dir → no-source mode
        if code_dir:
            system_prompt += (
                f"\n\n# ADDON SOURCE DIRECTORY\nThe Odoo addon source is at: {code_dir}\n"
                "Read its files from that absolute path (Read with absolute paths; "
                f"pass path={code_dir} to Grep/Glob)."
            )
        system_prompt = system_prompt[:_MAX_SYSTEM_PROMPT]

        cmd = [
            self.cli_path, "-p", prompt,
            "--output-format", "json",
            "--permission-mode", "bypassPermissions",
            "--allowedTools", _READONLY_TOOLS,
        ]
        if system_prompt:
            cmd += ["--append-system-prompt", system_prompt]
        if code_dir:
            cmd += ["--add-dir", code_dir]
        for d in (extra_dirs or []):
            if d and Path(d).is_dir():
                cmd += ["--add-dir", d]
        if resume:
            cmd += ["--resume", resume]

        env = dict(os.environ)
        if os.environ.get("SENTINEL_FORCE_SUBSCRIPTION", "1") != "0":
            env.pop("ANTHROPIC_API_KEY", None)  # → bill the subscription, not the API

        try:
            proc = subprocess.run(
                cmd, cwd=_neutral_workspace(), env=env,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise EngineUnavailable(f"Claude Code run exceeded {timeout}s and was stopped") from None
        except OSError as exc:
            raise EngineUnavailable(f"could not launch Claude Code: {exc}") from exc

        out = (proc.stdout or "").strip()
        if not out:
            raise EngineUnavailable((proc.stderr or "Claude Code produced no output").strip()[:400])

        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return EngineResult(text=out)  # plain text fallback

        return EngineResult(
            text=data.get("result") or data.get("text") or "(no result)",
            session_id=data.get("session_id"),
            cost_usd=data.get("total_cost_usd"),
            is_error=bool(data.get("is_error")),
        )

    # --- streaming -------------------------------------------------------

    def run_stream(
        self, prompt: str, *, code_dir: str | None = None, extra_dirs: list[str] | None = None,
        system_prompt: str = "", resume: str | None = None, timeout: int = 900,
    ) -> Iterator[dict]:
        """Yield progress events as Claude Code works. Event shapes:
            {"type":"text","text": ...}      assistant prose (stream into the bubble)
            {"type":"tool","name":..,"input":..}  a tool call (Read/Grep/Glob) — progress
            {"type":"result","session_id":..,"cost_usd":..,"is_error":..,"result":..}
            {"type":"error","message":..}
        """
        if not self.available():
            raise EngineUnavailable("`claude` CLI not found (npm install -g @anthropic-ai/claude-code)")

        code_dir = code_dir if (code_dir and Path(code_dir).is_dir()) else None  # no dir → no-source mode
        if code_dir:
            system_prompt += (
                f"\n\n# ADDON SOURCE DIRECTORY\nThe Odoo addon source is at: {code_dir}\n"
                "Read its files from that absolute path (Read with absolute paths; "
                f"pass path={code_dir} to Grep/Glob)."
            )
        system_prompt = system_prompt[:_MAX_SYSTEM_PROMPT]

        cmd = [
            self.cli_path, "-p", prompt,
            "--output-format", "stream-json", "--verbose",
            "--permission-mode", "bypassPermissions",
            "--allowedTools", _READONLY_TOOLS,
        ]
        if system_prompt:
            cmd += ["--append-system-prompt", system_prompt]
        if code_dir:
            cmd += ["--add-dir", code_dir]
        for d in (extra_dirs or []):
            if d and Path(d).is_dir():
                cmd += ["--add-dir", d]
        if resume:
            cmd += ["--resume", resume]

        env = dict(os.environ)
        if os.environ.get("SENTINEL_FORCE_SUBSCRIPTION", "1") != "0":
            env.pop("ANTHROPIC_API_KEY", None)

        try:
            proc = subprocess.Popen(
                cmd, cwd=_neutral_workspace(), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
        except OSError as exc:
            yield {"type": "error", "message": f"could not launch Claude Code: {exc}"}
            return

        killer = threading.Timer(timeout, proc.kill)
        killer.daemon = True
        killer.start()
        saw_result = False
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype = ev.get("type")
                if etype == "assistant":
                    for block in ev.get("message", {}).get("content", []):
                        if block.get("type") == "text" and block.get("text", "").strip():
                            yield {"type": "text", "text": block["text"]}
                        elif block.get("type") == "tool_use":
                            yield {"type": "tool", "name": block.get("name"),
                                   "input": block.get("input", {})}
                elif etype == "result":
                    saw_result = True
                    yield {"type": "result", "session_id": ev.get("session_id"),
                           "cost_usd": ev.get("total_cost_usd"),
                           "is_error": bool(ev.get("is_error")), "result": ev.get("result", "")}
        finally:
            killer.cancel()
            if proc.poll() is None:
                proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        if not saw_result:
            yield {"type": "error",
                   "message": "Claude Code ended without a result (timeout or startup error)."}
