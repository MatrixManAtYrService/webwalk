"""Tests for kge-format knowledge-graph generation (webwalk/graph/kge.py)."""

from __future__ import annotations

import json

from webwalk.graph.kge import states_to_graph, write_kge_graph

STATES = [
    {"id": "home", "url_contains": "/home"},
    {"id": "done", "title_contains": "Done", "terminal": True},
]
RULES = [
    {"id": "go", "when": "home", "do": {"ui": {"action": "click"}}, "expect": "done"},
    {"id": "disc", "when": "done", "do": {"http": {}}},  # no expect → a discovery edge
]


def test_states_become_page_nodes_with_terminal_flag() -> None:
    g = states_to_graph("demo", STATES, RULES)
    by_id = {n["id"]: n for n in g["nodes"]}
    assert by_id["page:home"]["type"] == "page"
    assert by_id["page:home"]["data"]["url_contains"] == "/home"
    assert by_id["page:done"]["data"]["terminal"] is True


def test_rules_become_edges_with_kind_and_rule_id() -> None:
    g = states_to_graph("demo", STATES, RULES)
    nav = next(e for e in g["edges"] if e["type"] == "NAVIGATES_TO")
    assert (nav["from"], nav["to"]) == ("page:home", "page:done")
    assert nav["data"] == {"rule": "go", "kind": "ui", "source": "capture"}


def test_discovery_rule_makes_frontier_node_and_edge() -> None:
    g = states_to_graph("demo", STATES, RULES)
    assert any(n["id"] == "page:__frontier__" and n["type"] == "frontier" for n in g["nodes"])
    disc = next(e for e in g["edges"] if e["type"] == "DISCOVERS")
    assert disc["to"] == "page:__frontier__"
    assert disc["data"]["discovery"] is True


def test_multi_action_rule_kind() -> None:
    rules = [{"id": "m", "when": "home", "do": [{"http": {}}, {"ui": {}}], "expect": "done"}]
    g = states_to_graph("demo", STATES, rules)
    nav = next(e for e in g["edges"] if e["type"] == "NAVIGATES_TO")
    assert nav["data"]["kind"] == "multi"


def test_write_kge_graph_files_and_shape(tmp_path) -> None:
    g = states_to_graph("demo", STATES, RULES)
    res = write_kge_graph(tmp_path, g)
    assert res["nodes"] > 0 and res["edges"] > 0
    schema = json.loads((tmp_path / "schema.json").read_text())
    assert "page" in schema["nodeTypes"] and "NAVIGATES_TO" in schema["edgeTypes"]
    # nodes sorted by id; edges sorted by (from, type, to) — clean diffs
    nodes = json.loads((tmp_path / "nodes.json").read_text())["nodes"]
    assert [n["id"] for n in nodes] == sorted(n["id"] for n in nodes)


def test_regenerate_preserves_curated_semantic_layer(tmp_path) -> None:
    g = states_to_graph("demo", STATES, RULES)
    write_kge_graph(tmp_path, g)

    # A human adds a semantic-family node + edge in kge.
    nodes = json.loads((tmp_path / "nodes.json").read_text())["nodes"]
    nodes.append({"id": "entity:session", "type": "entity", "label": "Session", "data": {"source": "curated"}})
    (tmp_path / "nodes.json").write_text(json.dumps({"nodes": nodes}))
    edges = json.loads((tmp_path / "edges.json").read_text())["edges"]
    edges.append({"type": "REPRESENTS", "from": "entity:session", "to": "page:home", "data": {}})
    (tmp_path / "edges.json").write_text(json.dumps({"edges": edges}))

    # Re-walking regenerates the acquisition layer; curation must survive.
    write_kge_graph(tmp_path, g)
    nodes2 = json.loads((tmp_path / "nodes.json").read_text())["nodes"]
    edges2 = json.loads((tmp_path / "edges.json").read_text())["edges"]
    assert any(n["id"] == "entity:session" for n in nodes2)      # semantic node kept
    assert any(e["type"] == "REPRESENTS" for e in edges2)        # semantic edge kept
    assert any(n["id"] == "page:home" for n in nodes2)           # acquisition regenerated
