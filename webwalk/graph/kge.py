"""Emit a flow's state graph as a kge-format knowledge graph.

`states_to_graph` is pure — it turns a flow's `states`/`rules` (the same data
`build_machine()` enumerates in `engine/runner.py`) into the `{schema, nodes,
edges}` shape kge stores on disk. `write_kge_graph` merges that projection into a
graph directory, clobbering only the generated (acquisition) types so any
human-curated semantic nodes/edges survive.

Node shape (kge):  ``{"id", "type", "label", "data"}``
Edge shape (kge):  ``{"type", "from", "to", "data"}``   (``from``/``to`` on disk)
Schema shape:      ``{"nodeTypes", "edgeTypes", "colorKey", "colorValues"}``
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from webwalk.engine.runner import UNKNOWN, _expect_list

# The acquisition-family vocabulary this module authors. Regenerating a graph
# rewrites exactly these node/edge types and leaves everything else (i.e. any
# `semantic`-family types a human added in kge) in place.
NODE_TYPES: dict[str, dict[str, Any]] = {
    "page": {
        "color": "#5b7fa6",
        "description": "A recognized page/course state (one state signature).",
        "family": "acquisition",
    },
    "frontier": {
        "color": "#9aa0a6",
        "description": "An as-yet-undiscovered state a discovery rule lands on (the UNKNOWN sentinel).",
        "family": "acquisition",
    },
}
EDGE_TYPES: dict[str, dict[str, Any]] = {
    "NAVIGATES_TO": {
        "color": "#4e79a7",
        "description": "A rule transition between states (source `when` → dest `expect`).",
        "family": "acquisition",
    },
    "DISCOVERS": {
        "color": "#bab0ac",
        "description": "A discovery rule with no known destination yet (lands on the frontier).",
        "family": "acquisition",
    },
}
GENERATED_NODE_TYPES = frozenset(NODE_TYPES)
GENERATED_EDGE_TYPES = frozenset(EDGE_TYPES)

_FRONTIER_ID = "page:__frontier__"


def _rule_kind(do: Any) -> str:
    """Classify a rule's `do` for the edge's `data.kind` (documentation/round-trip):
    a list of actions is `multi`; a single action dict is named by its key."""
    if isinstance(do, list):
        return "multi"
    if isinstance(do, dict):
        for key in ("ui", "http", "eval", "mission", "challenge", "coding_lab"):
            if key in do:
                return key
    return "unknown"


def states_to_graph(
    flow_id: str, state_defs: list[dict[str, Any]], rule_defs: list[dict[str, Any]]
) -> dict[str, Any]:
    """Project a flow's states + rules into a kge `{schema, nodes, edges}` graph.

    States become `page` nodes; each rule becomes one edge per destination in its
    `expect` (a rule with no `expect` is a `DISCOVERS` edge into the frontier — the
    map's discovery front, marked `data.discovery`). A state referenced by a rule
    but never declared is emitted too, flagged `data.undeclared` so the gap shows.
    Every generated item carries `data.source = "capture"` so kge can color the
    machine-authored layer distinctly from curated (`source = "curated"`) nodes.
    """
    nodes: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_node(nid: str, ntype: str, label: str, data: dict[str, Any]) -> None:
        # First writer wins: declared states are added before rule-referenced ones,
        # so a declared node's richer data is never overwritten by an undeclared stub.
        if nid in seen:
            return
        seen.add(nid)
        nodes.append({"id": nid, "type": ntype, "label": label, "data": data})

    terminals = {s.get("id") for s in state_defs if s.get("terminal")}
    for s in state_defs:
        sid = s.get("id")
        if not sid:
            continue
        data: dict[str, Any] = {"source": "capture"}
        for key in ("url_contains", "title_contains"):
            if s.get(key):
                data[key] = s[key]
        if sid in terminals:
            data["terminal"] = True
        add_node(f"page:{sid}", "page", sid, data)

    edges: list[dict[str, Any]] = []
    need_frontier = False
    for r in rule_defs:
        when = r.get("when")
        if not when:
            continue
        src = f"page:{when}"
        add_node(src, "page", when, {"source": "capture", "undeclared": True})
        kind = _rule_kind(r.get("do"))
        for dest in _expect_list(r.get("expect")):
            if dest == UNKNOWN:
                need_frontier = True
                edges.append({
                    "type": "DISCOVERS", "from": src, "to": _FRONTIER_ID,
                    "data": {"rule": r.get("id"), "kind": kind, "discovery": True, "source": "capture"},
                })
            else:
                add_node(f"page:{dest}", "page", dest, {"source": "capture", "undeclared": True})
                edges.append({
                    "type": "NAVIGATES_TO", "from": src, "to": f"page:{dest}",
                    "data": {"rule": r.get("id"), "kind": kind, "source": "capture"},
                })

    if need_frontier:
        add_node(_FRONTIER_ID, "frontier", "(frontier)", {"source": "capture"})

    schema = {
        "nodeTypes": {k: dict(v) for k, v in NODE_TYPES.items()},
        "edgeTypes": {k: dict(v) for k, v in EDGE_TYPES.items()},
        "colorKey": "source",
        "colorValues": {"capture": "#5b7fa6", "curated": "#59a14f"},
    }
    return {"flow": flow_id, "schema": schema, "nodes": nodes, "edges": edges}


def _read_json(path: Path) -> dict[str, Any] | None:
    return json.loads(path.read_text()) if path.exists() else None


def _dump(path: Path, obj: Any) -> None:
    """Write sorted, pretty JSON via temp-then-rename, so files diff cleanly in git
    (matching kge's on-disk discipline)."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    tmp.replace(path)


def write_kge_graph(
    out_dir: str | Path,
    graph: dict[str, Any],
    *,
    regen_node_types: frozenset[str] = GENERATED_NODE_TYPES,
    regen_edge_types: frozenset[str] = GENERATED_EDGE_TYPES,
) -> dict[str, Any]:
    """Merge `graph` (from `states_to_graph`) into a kge graph directory.

    Clobber is scoped by type: existing nodes/edges whose `type` is in the
    regenerated set are replaced; every other type (i.e. curated `semantic` items)
    is preserved. Schema types union (generated definitions win); an existing
    `colorKey`/`colorValues` is kept so a human's color choices stick.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    existing_schema = _read_json(out / "schema.json") or {}
    schema = {
        "nodeTypes": {**existing_schema.get("nodeTypes", {}), **graph["schema"]["nodeTypes"]},
        "edgeTypes": {**existing_schema.get("edgeTypes", {}), **graph["schema"]["edgeTypes"]},
        "colorKey": existing_schema.get("colorKey") or graph["schema"].get("colorKey", ""),
        "colorValues": {**graph["schema"].get("colorValues", {}), **existing_schema.get("colorValues", {})},
    }

    existing_nodes = (_read_json(out / "nodes.json") or {}).get("nodes", [])
    by_id: dict[str, dict[str, Any]] = {
        n["id"]: n for n in existing_nodes if n.get("type") not in regen_node_types
    }
    for n in graph["nodes"]:
        by_id[n["id"]] = n
    nodes = sorted(by_id.values(), key=lambda n: n.get("id", ""))

    existing_edges = (_read_json(out / "edges.json") or {}).get("edges", [])
    kept_edges = [e for e in existing_edges if e.get("type") not in regen_edge_types]
    edges = sorted(
        kept_edges + graph["edges"],
        key=lambda e: (e.get("from", ""), e.get("type", ""), e.get("to", "")),
    )

    _dump(out / "schema.json", schema)
    _dump(out / "nodes.json", {"nodes": nodes})
    _dump(out / "edges.json", {"edges": edges})
    return {"dir": str(out), "nodes": len(nodes), "edges": len(edges)}
