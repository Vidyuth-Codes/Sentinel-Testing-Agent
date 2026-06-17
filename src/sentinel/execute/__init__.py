"""Phase 3 — RPC flow executor.

Generate executable RPC test cases with Claude Code (`generate_cases`), provision a safe
(cloned) database (`provision`/`teardown`), run the cases deterministically over XML-RPC
(`run_cases`), and render a Test Results report (`write_report`). See LLD §10.
"""

from sentinel.execute.generate import generate_cases
from sentinel.execute.models import ExecCase, ExecCaseSet, ExecReport, UIPageResult, UIReport
from sentinel.execute.provision import Provisioned, master_password, provision, teardown
from sentinel.execute.report import render_markdown, write_report, write_ui_report
from sentinel.execute.runner import run_case, run_cases
from sentinel.execute.ui_playwright import PlaywrightUnavailable, run_ui_crawl

__all__ = [
    "generate_cases",
    "ExecCase",
    "ExecCaseSet",
    "ExecReport",
    "UIPageResult",
    "UIReport",
    "Provisioned",
    "master_password",
    "provision",
    "teardown",
    "render_markdown",
    "write_report",
    "write_ui_report",
    "run_case",
    "run_cases",
    "PlaywrightUnavailable",
    "run_ui_crawl",
]
