"""The Secure Code Warrior flow, packaged as a `Flow` descriptor.

`FLOW` is what `scw-sim` runs and what the `webwalk.flows` entry point exposes, so
`webwalk --flow scw run` works once webwalk is installed. Its *knowledge* (states,
rules, answer keys) ships in `data/` beside this file; its *mechanics* come from
`reader.py`. An external repo (e.g. ww-kg-clover) can reuse the mechanics via the
manifest's dotted-path plugins while pointing `data:` at its own copies.
"""

from __future__ import annotations

from pathlib import Path

from webwalk.flow import Flow
from webwalk.flows.scw.reader import build_subflows, register_cli

_DATA = Path(__file__).resolve().parent / "data"

FLOW = Flow(
    id="scw",
    description=(
        "scw-sim — drive Secure Code Warrior training: browser verbs (connect / observe / step / …), "
        "a `run` walker that resumes the course from wherever you are, a `graph` exporter, and "
        "`mission` / `challenge` readers."
    ),
    data={
        "states": _DATA / "states.yaml",
        "missions": _DATA / "missions.yaml",
        "challenges": _DATA / "challenges.yaml",
        "coding_labs": _DATA / "coding-labs.yaml",
    },
    server_plugin="webwalk.flows.scw.observers",
    subflow_factory=build_subflows,
    cli_register=register_cli,
    root=Path(__file__).resolve().parent,
)

__all__ = ["FLOW"]
