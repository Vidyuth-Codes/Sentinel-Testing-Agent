"""Sentinel — agentic QA & bug-detection agent for Odoo 18.

Phase 1 (built): deterministic Odoo tools — live XML-RPC introspection into a
System Map, addon source scan, understanding report — plus a FastAPI web UI
(chat + dashboard).

Phase 2 (next): reasoning via Claude Code (headless `claude -p`, subscription)
guided by the Odoo-QA skill — gap analysis, bug findings, and test-plan
generation, populating the shared `Finding` model.

Phase 3 (planned): RPC flow + Playwright UI executors running real flows against
a duplicate DB inside a Docker sandbox.
"""

__version__ = "0.2.0"
