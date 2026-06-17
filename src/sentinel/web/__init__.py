"""Sentinel web UI — FastAPI backend + single-page frontend.

Architecture (Claude Code era):
  Browser (this SPA)
      -> FastAPI (this module)
          - /api/introspect : DETERMINISTIC Odoo tools (no LLM key) — live System Map
          - /api/chat       : the REASONING engine = Claude Code (headless `claude -p`)
                              with a safe mock fallback when Claude Code isn't wired yet.
"""

from sentinel.web.app import app

__all__ = ["app"]
