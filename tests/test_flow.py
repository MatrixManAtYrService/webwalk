"""Tests for flow discovery + manifest loading (webwalk/flow.py)."""

from __future__ import annotations

import json

from webwalk.flow import discover, do_graph, load_flow, resolve

STATES = """
api_hosts: [example.com]
states:
  - id: home
    url_contains: example.com/home
  - id: done
    url_contains: example.com/done
    terminal: true
rules:
  - id: go
    when: home
    do: { ui: { action: click, selector: "a.next" } }
    expect: done
"""


def _write_flow(base, *, plugins: str = "") -> None:
    (base / "data").mkdir(exist_ok=True)
    (base / "data" / "states.yaml").write_text(STATES)
    manifest = "id: demo\ndescription: Demo flow\ndata:\n  states: data/states.yaml\n"
    (base / "flow.yaml").write_text(manifest + plugins)


def test_pure_data_flow_loads_without_plugins(tmp_path) -> None:
    _write_flow(tmp_path)
    f = load_flow(tmp_path)
    assert f.id == "demo"
    assert f.subflow_factory is None      # pure-data: a plain page-state walk
    assert f.cli_register is None
    assert f.server_plugin == ""
    assert f.data["states"].name == "states.yaml"
    assert f.data["states"].is_absolute()


def test_server_plugin_kept_as_string_not_imported(tmp_path) -> None:
    # A server plugin that doesn't exist must NOT be imported at load time (the
    # server process imports it) — loading should still succeed.
    _write_flow(tmp_path, plugins="plugins:\n  server: no.such.module\n")
    f = load_flow(tmp_path)
    assert f.server_plugin == "no.such.module"


def test_resolve_by_path(tmp_path) -> None:
    _write_flow(tmp_path)
    assert resolve(str(tmp_path)).id == "demo"


def test_do_graph_writes_kge_files(tmp_path) -> None:
    _write_flow(tmp_path)
    flow = load_flow(tmp_path)
    out = tmp_path / "graphs" / "demo"
    do_graph(flow, str(out))
    schema = json.loads((out / "schema.json").read_text())
    assert "page" in schema["nodeTypes"]
    nodes = json.loads((out / "nodes.json").read_text())["nodes"]
    assert any(n["id"] == "page:home" for n in nodes)


def test_scw_discovered_via_entry_point() -> None:
    flows = discover()
    assert "scw" in flows, "the scw flow should be registered under the webwalk.flows entry point"
    assert flows["scw"].subflow_factory is not None   # code flow: has plugins
    assert flows["scw"].server_plugin == "webwalk.flows.scw.observers"
