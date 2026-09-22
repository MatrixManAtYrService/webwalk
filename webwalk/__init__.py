"""webwalk — a flow-agnostic harness that drives a Selenium browser through a web
flow (observe → match → act → verify) and captures what it finds.

This module is the **public SDK** an external flow imports. A flow repo depends on
`webwalk`, ships a `flow.yaml` (+ optional plugins), and its CLI is a one-liner::

    from webwalk import run_flow_cli, load_flow
    from pathlib import Path
    def main() -> None:
        run_flow_cli(load_flow(Path(__file__).resolve().parent.parent))

See `webwalk/flow.py` for the flow contract and `webwalk/graph/` for knowledge-graph
capture.
"""

from __future__ import annotations

from webwalk.cli._walk import execute_walk, load_course, make_callbacks
from webwalk.cli.core import build_app, register_core_commands
from webwalk.engine.matcher import match_states
from webwalk.engine.subflow import SubFlow
from webwalk.flow import Flow, discover, load_flow, resolve, run_flow_cli
from webwalk.graph import model_to_graph, states_to_graph, write_kge_graph
from webwalk.server.plugins import flow_router, register_snapshot_extension

__all__ = [
    # flow contract
    "Flow",
    "load_flow",
    "discover",
    "resolve",
    "run_flow_cli",
    # CLI building blocks
    "build_app",
    "register_core_commands",
    # walk plumbing
    "execute_walk",
    "load_course",
    "make_callbacks",
    "match_states",
    "SubFlow",
    # server plugins
    "register_snapshot_extension",
    "flow_router",
    # knowledge-graph capture
    "states_to_graph",
    "model_to_graph",
    "write_kge_graph",
]
