"""scw-sim — the Secure Code Warrior flow CLI.

Thin by design: the whole flow (its data paths, server plugin, sub-flows, and
extra verbs) is described by `FLOW`, and `run_flow_cli` builds the CLI from it —
the core browser verbs (connect / observe / step / …) + the generic flow verbs
(`where` / `run` / `graph`) + SCW's own `mission` / `challenge`. The SCW-specific
code lives in `webwalk/flows/scw/reader.py`; the knowledge lives in `data/`.
"""

from __future__ import annotations

from webwalk.flow import run_flow_cli
from webwalk.flows.scw import FLOW


def main() -> None:
    run_flow_cli(FLOW)


if __name__ == "__main__":
    main()
