"""Project a flow's declarative data model into a kge graph.

Where `states_to_graph` captures *navigation* ("where to find it"), this captures
the *data model* ("what it is"): the API **services** and **endpoints** a flow
talks to, the domain **entities** they yield, and the **relationships** between
those entities. A flow ships it as a `data/model.yaml` beside its states, so the
knowledge stays data (not code) and lives in the flow repo.

The model layer uses its own node/edge *families* (`model`), so `graph`
regenerates it independently of the state layer and of anything a human curates in
kge — the same type-scoped clobber `write_kge_graph` applies to the state layer.

`model.yaml` shape::

    services:
      - { id: tempo, host: jira.corp.clover.com, base: /rest/tempo-timesheets/4,
          description: "Tempo timesheets REST API" }
    endpoints:
      - { id: tempo-worklog-create, service: tempo, method: POST, path: /worklogs/,
          returns: [worklog], description: "Create a worklog (originTaskId = issue id)" }
    entities:
      - { id: worklog, label: Worklog, attributes: [day, key, originTaskId, timeSpentSeconds],
          description: "A Tempo worklog" }
    edges:                                  # entity→entity relationships
      - { from: worklog, to: jira-issue, type: LOGS_AGAINST, note: "originTaskId = issue id" }
"""

from __future__ import annotations

from typing import Any

MODEL_NODE_TYPES: dict[str, dict[str, Any]] = {
    "service": {"color": "#76b7b2", "description": "An API host/service the flow talks to.", "family": "model"},
    "endpoint": {"color": "#f28e2b", "description": "An API endpoint (method + path).", "family": "model"},
    "entity": {"color": "#4e79a7", "description": "A domain object the flow reads or writes.", "family": "model"},
}

# Known edge types get a stable color + description; anything else a flow invents
# falls back to a neutral default so the graph still validates in kge.
_EDGE_META: dict[str, dict[str, str]] = {
    "HOSTED_BY": {"color": "#bab0ac", "description": "Endpoint is served by this service."},
    "RETURNS": {"color": "#4e79a7", "description": "Endpoint yields this entity."},
    "REFERENCES": {"color": "#e15759", "description": "Entity references another (e.g. a key parsed from text)."},
    "IN_PROJECT": {"color": "#9c755f", "description": "Entity belongs to this project."},
    "ALLOCATED_TO": {"color": "#edc948", "description": "Source entity is allocated/attributed to the target."},
    "LOGS_AGAINST": {"color": "#59a14f", "description": "A worklog logs time against this issue."},
    "IN_PERIOD": {"color": "#76b7b2", "description": "Entity falls within this time period."},
    "REQUIRES": {"color": "#b07aa1", "description": "Period requires these per-day hours."},
    "HAS_TRANSITION": {"color": "#ff9da7", "description": "Issue has this workflow transition."},
}
_DEFAULT_EDGE_COLOR = "#9aa0a6"


def _ref(ref: str) -> str:
    """Resolve a model edge endpoint to a node id. Bare names are entities; a
    prefixed name (`endpoint:x` / `svc:x`) is used as-is for cross-kind links."""
    return ref if ":" in ref else f"entity:{ref}"


def model_to_graph(flow_id: str, model: dict[str, Any]) -> dict[str, Any]:
    """Project a `model.yaml` dict into a kge `{schema, nodes, edges}` graph.

    Services/endpoints/entities become nodes; `HOSTED_BY` links each endpoint to
    its service, `RETURNS` links it to the entities it yields, and each `edges`
    entry is an entity→entity relationship. Everything is stamped
    `data.source = "model"` so kge can color the data-model layer distinctly.
    """
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    edge_types_used: set[str] = set()

    for s in model.get("services") or []:
        sid = s.get("id")
        if not sid:
            continue
        nodes.append({
            "id": f"svc:{sid}", "type": "service", "label": s.get("label") or sid,
            "data": {"source": "model", "host": s.get("host"), "base": s.get("base"),
                     "description": s.get("description")},
        })

    for e in model.get("endpoints") or []:
        eid = e.get("id")
        if not eid:
            continue
        nodes.append({
            "id": f"endpoint:{eid}", "type": "endpoint", "label": e.get("label") or eid,
            "data": {"source": "model", "method": e.get("method"), "path": e.get("path"),
                     "service": e.get("service"), "description": e.get("description")},
        })
        if e.get("service"):
            edges.append({"type": "HOSTED_BY", "from": f"endpoint:{eid}",
                          "to": f"svc:{e['service']}", "data": {"source": "model"}})
            edge_types_used.add("HOSTED_BY")
        for ent in e.get("returns") or []:
            edges.append({"type": "RETURNS", "from": f"endpoint:{eid}",
                          "to": f"entity:{ent}", "data": {"source": "model"}})
            edge_types_used.add("RETURNS")

    for en in model.get("entities") or []:
        enid = en.get("id")
        if not enid:
            continue
        nodes.append({
            "id": f"entity:{enid}", "type": "entity", "label": en.get("label") or enid,
            "data": {"source": "model", "attributes": en.get("attributes"),
                     "description": en.get("description")},
        })

    for r in model.get("edges") or []:
        t = r.get("type")
        if not t or "from" not in r or "to" not in r:
            continue
        edge_types_used.add(t)
        edges.append({"type": t, "from": _ref(r["from"]), "to": _ref(r["to"]),
                      "data": {"source": "model", "note": r.get("note")}})

    edge_types = {
        t: {"color": _EDGE_META.get(t, {}).get("color", _DEFAULT_EDGE_COLOR),
            "description": _EDGE_META.get(t, {}).get("description", "A modeled relationship."),
            "family": "model"}
        for t in sorted(edge_types_used)
    }
    schema = {
        "nodeTypes": {k: dict(v) for k, v in MODEL_NODE_TYPES.items()},
        "edgeTypes": edge_types,
        "colorKey": "source",
        "colorValues": {"capture": "#5b7fa6", "model": "#af7aa1", "curated": "#59a14f"},
    }
    return {"flow": flow_id, "schema": schema, "nodes": nodes, "edges": edges}
