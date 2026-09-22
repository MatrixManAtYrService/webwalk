"""Flow discovery + the generic flow CLI — the seam that lets a flow live outside.

A **flow** teaches webwalk one site: its state signatures, rules, and (optionally)
its imperative extensions. The core stays flow-agnostic; a flow is loaded, not
baked in. There are two kinds:

  * **pure-data** — just a `flow.yaml` naming a `data/states.yaml`. No Python. The
    generic `where`/`run`/`graph` verbs work over it directly.
  * **code** — the manifest also names plugins (dotted paths): a server snapshot
    observer, a sub-flow factory, and extra CLI verbs. SCW is the worked example.

Discovery mirrors the sibling `knowledge-graph-editor` (convention over
registration), adapted for "data + optional code":

  * **entry points** — an installed flow package registers a `Flow` under the
    `webwalk.flows` entry-point group; `webwalk --flow <id>` finds it.
  * **path / manifest** — `--flow <dir>` (or `WEBWALK_FLOWS`, or a `flow.yaml` /
    `flows/*/flow.yaml` in the cwd) loads a flow straight from disk, no install.

The `Flow` *type* lives here in the core; `Flow` *instances* live in flow repos.
A flow repo's own thin CLI is just ``run_flow_cli(FLOW)``.

Manifest (`flow.yaml`)::

    id: scw
    description: Secure Code Warrior training
    api_hosts: [securecodewarrior.com]
    data:
      states:   data/states.yaml          # required
      missions: data/missions.yaml         # flow-specific extras, optional
      challenges: data/challenges.yaml
      coding_labs: data/coding-labs.yaml
    plugins:                               # omit entirely for a pure-data flow
      server:   webwalk.flows.scw.observers          # → WEBWALK_SERVER_PLUGINS
      subflows: webwalk.flows.scw.reader:build_subflows
      cli:      webwalk.flows.scw.reader:register_cli
"""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from webwalk.cli._walk import execute_walk, load_course, make_callbacks
from webwalk.engine.matcher import match_states
from webwalk.graph import states_to_graph, write_kge_graph

if TYPE_CHECKING:
    from webwalk.cli._walk import WalkCallbacks
    from webwalk.engine.subflow import SubFlow

ENTRY_POINT_GROUP = "webwalk.flows"
MANIFEST_NAME = "flow.yaml"

# A sub-flow factory: (flow, callbacks, window) -> the sub-flows a walk hands off
# to. Named by dotted path in the manifest's `plugins.subflows`.
SubFlowFactory = Callable[["Flow", "WalkCallbacks", str], "list[SubFlow]"]
# Extra CLI verbs a flow contributes (e.g. SCW's `mission`/`challenge`), named by
# `plugins.cli`. It mutates the Typer app in place.
CliRegister = Callable[[typer.Typer, "Flow"], None]


@dataclass
class Flow:
    """A resolved flow: its knowledge (data files) plus optional code plugins."""

    id: str
    description: str = ""
    data: dict[str, Path] = field(default_factory=dict)   # {"states": Path, "missions": Path, …}
    api_hosts: list[str] | None = None
    server_plugin: str = ""                                # dotted module path → WEBWALK_SERVER_PLUGINS
    subflow_factory: SubFlowFactory | None = None          # None → a plain page-state walk
    cli_register: CliRegister | None = None                # None → only the generic verbs
    root: Path | None = None                               # the flow directory (manifest location)

    @property
    def states_path(self) -> Path:
        """The required states file. Raises a friendly CLI error if it's missing."""
        p = self.data.get("states")
        if not p:
            typer.echo(f"Flow {self.id!r} declares no `data.states`.", err=True)
            raise typer.Exit(1)
        return p


def _import_attr(dotted: str) -> Any:
    """Import a ``module:attr`` (or ``module.attr``) reference to a callable/object."""
    if ":" in dotted:
        mod, _, attr = dotted.partition(":")
    else:
        mod, _, attr = dotted.rpartition(".")
    obj = importlib.import_module(mod)
    return getattr(obj, attr) if attr else obj


def load_flow(path: str | Path) -> Flow:
    """Load a flow from a `flow.yaml` manifest (or the directory containing one).

    Data paths resolve relative to the manifest; plugin dotted-paths are imported
    now (a pure-data flow with no `plugins:` imports nothing). The server plugin is
    kept as a string — the *server* process imports it, not the CLI.
    """
    import yaml

    p = Path(path)
    manifest = p / MANIFEST_NAME if p.is_dir() else p
    if not manifest.exists():
        typer.echo(f"No flow manifest at {manifest}", err=True)
        raise typer.Exit(1)
    doc = yaml.safe_load(manifest.read_text()) or {}
    root = manifest.parent

    data = {key: (root / rel).resolve() for key, rel in (doc.get("data") or {}).items()}
    plugins = doc.get("plugins") or {}
    subflows = plugins.get("subflows")
    cli = plugins.get("cli")

    return Flow(
        id=doc.get("id") or root.name,
        description=doc.get("description", ""),
        data=data,
        api_hosts=doc.get("api_hosts"),
        server_plugin=plugins.get("server", "") or "",
        subflow_factory=_import_attr(subflows) if subflows else None,
        cli_register=_import_attr(cli) if cli else None,
        root=root,
    )


def _scan_dir_for_flows(base: Path) -> dict[str, Flow]:
    """A flow.yaml directly in `base`, and any in `base/*/` (a `flows/` root)."""
    found: dict[str, Flow] = {}
    if (base / MANIFEST_NAME).exists():
        f = load_flow(base)
        found[f.id] = f
    for child in sorted(base.glob(f"*/{MANIFEST_NAME}")):
        f = load_flow(child)
        found.setdefault(f.id, f)
    return found


def discover() -> dict[str, Flow]:
    """All flows visible right now: installed (entry points) + on-disk (path scan).

    Entry points win over path-discovered flows of the same id. Path roots come
    from `WEBWALK_FLOWS` (`:`/`,`-separated) and the current directory (`./flow.yaml`
    and `./flows/*/flow.yaml`).
    """
    flows: dict[str, Flow] = {}
    for ep in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
        try:
            obj = ep.load()
        except Exception:  # a broken/optional flow shouldn't sink discovery
            continue
        flow = obj() if callable(obj) and not isinstance(obj, type) else obj
        if isinstance(flow, Flow):
            flows.setdefault(ep.name, flow)

    roots: list[Path] = []
    raw = os.environ.get("WEBWALK_FLOWS", "")
    roots += [Path(r) for r in raw.replace(",", ":").split(":") if r.strip()]
    roots += [Path.cwd(), Path.cwd() / "flows"]
    for base in roots:
        if base.is_dir():
            for fid, flow in _scan_dir_for_flows(base).items():
                flows.setdefault(fid, flow)
    return flows


def resolve(id_or_path: str) -> Flow:
    """Resolve a `--flow` value: a path to a manifest/dir, or a discovered flow id."""
    p = Path(id_or_path)
    if p.exists() or p.name == MANIFEST_NAME:
        return load_flow(p)
    flows = discover()
    if id_or_path in flows:
        return flows[id_or_path]
    known = ", ".join(sorted(flows)) or "(none)"
    typer.echo(f"Unknown flow {id_or_path!r}. Known: {known}. Or pass a path to a {MANIFEST_NAME}.", err=True)
    raise typer.Exit(1)


# --- the generic flow verbs (shared by a flow CLI and the bare `webwalk --flow`) ---


def _load_states(flow: Flow) -> list[dict[str, Any]]:
    import yaml

    path = flow.states_path
    if not path.exists():
        typer.echo(f"States file not found: {path}", err=True)
        raise typer.Exit(1)
    return (yaml.safe_load(path.read_text()) or {}).get("states", [])


def do_where(flow: Flow, window: str = "") -> None:
    """Observe the browser and report which of the flow's known state(s) match."""
    from webwalk.cli._shared import _call

    state_defs = _load_states(flow)
    result = _call("/observe", json={"label": "where", "window": window or None})
    payload = (result.get("record") or {}).get("state") or {}
    matched = match_states(state_defs, payload)
    typer.echo(json.dumps(
        {"matched": matched, "unambiguous": len(matched) == 1,
         "url": payload.get("url"), "title": payload.get("title")},
        indent=2, default=str,
    ))
    if not matched:
        raise typer.Exit(1)


def do_run(flow: Flow, *, to: str = "", window: str = "", once: bool = False,
           yes: bool = False, brief: bool = False) -> None:
    """Walk the flow's state graph: observe → match → act → verify, resumable."""
    state_defs, rule_defs, api_hosts = load_course(str(flow.states_path))
    if flow.api_hosts:
        api_hosts = flow.api_hosts
    cb = make_callbacks(window)
    subflows = flow.subflow_factory(flow, cb, window) if flow.subflow_factory else []
    execute_walk(
        state_defs, rule_defs, api_hosts, subflows=subflows,
        callbacks=cb, to=to, once=once, yes=yes, brief=brief,
    )


def do_graph(flow: Flow, out: str = "") -> None:
    """Generate/refresh the flow's kge-format acquisition graph (states + rules)."""
    out_dir = out or f"./graphs/{flow.id}"
    state_defs, rule_defs, _ = load_course(str(flow.states_path))
    graph = states_to_graph(flow.id, state_defs, rule_defs)
    result = write_kge_graph(out_dir, graph)
    typer.echo(json.dumps(result, indent=2, default=str))
    typer.echo(f"Serve it with:  kge serve   (from {Path(out_dir).parent})", err=True)


def bind_flow_commands(app: typer.Typer, flow: Flow) -> None:
    """Attach `where`/`run`/`graph` bound to a single, already-resolved flow."""

    @app.command()
    def where(window: Annotated[str, typer.Option(help="Observe this window (index/'latest'/url substring)")] = "") -> None:  # noqa: E501
        """Observe the browser and report which known flow state(s) you're in."""
        do_where(flow, window)

    @app.command()
    def run(
        to: Annotated[str, typer.Option(help="Walk until this state id (else until no rule applies)")] = "",
        window: Annotated[str, typer.Option(help="Observe/act on this window (index/'latest'/url substring)")] = "",
        once: Annotated[bool, typer.Option(help="Take a single step, then stop")] = False,
        yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't prompt before each step (autonomous)")] = False,
        brief: Annotated[bool, typer.Option("--brief", help="Compact text summary instead of full JSON")] = False,
    ) -> None:
        """Walk the flow's state graph: observe → match → act → verify, resumable."""
        do_run(flow, to=to, window=window, once=once, yes=yes, brief=brief)

    @app.command()
    def graph(
        out: Annotated[str, typer.Option(help="Output graph dir (default ./graphs/<flow>)")] = "",
    ) -> None:
        """Emit this flow's state graph as a kge knowledge graph (browse it in kge)."""
        do_graph(flow, out)


# Module-level so Typer's `get_type_hints` can resolve it inside the annotations
# below (under `from __future__ import annotations`, annotation strings are
# re-evaluated in module globals, not the enclosing function's locals).
_FLOW_OPT = typer.Option(..., "--flow", help=f"Flow id (installed) or path to a {MANIFEST_NAME}/dir")


def add_generic_flow_commands(app: typer.Typer) -> None:
    """Attach `where`/`run`/`graph` that take a `--flow <id|path>` (for bare `webwalk`)."""

    @app.command()
    def where(
        flow: Annotated[str, _FLOW_OPT],
        window: Annotated[str, typer.Option(help="Observe this window (index/'latest'/url substring)")] = "",
    ) -> None:
        """Observe the browser and report which known flow state(s) you're in."""
        do_where(resolve(flow), window)

    @app.command()
    def run(
        flow: Annotated[str, _FLOW_OPT],
        to: Annotated[str, typer.Option(help="Walk until this state id (else until no rule applies)")] = "",
        window: Annotated[str, typer.Option(help="Observe/act on this window (index/'latest'/url substring)")] = "",
        once: Annotated[bool, typer.Option(help="Take a single step, then stop")] = False,
        yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't prompt before each step (autonomous)")] = False,
        brief: Annotated[bool, typer.Option("--brief", help="Compact text summary instead of full JSON")] = False,
    ) -> None:
        """Walk a flow's state graph: observe → match → act → verify, resumable."""
        do_run(resolve(flow), to=to, window=window, once=once, yes=yes, brief=brief)

    @app.command()
    def graph(
        flow: Annotated[str, _FLOW_OPT],
        out: Annotated[str, typer.Option(help="Output graph dir (default ./graphs/<flow>)")] = "",
    ) -> None:
        """Emit a flow's state graph as a kge knowledge graph (browse it in kge)."""
        do_graph(resolve(flow), out)


def run_flow_cli(flow: Flow) -> None:
    """Build and run a flow's CLI: the core browser verbs + the generic flow verbs
    + whatever extra verbs the flow's `plugins.cli` registers. A flow repo's own
    console-script entry point is just ``run_flow_cli(FLOW)``."""
    from webwalk.cli.core import build_app

    app = build_app(
        flow.description or f"{flow.id} — webwalk flow: browser verbs + a `run` walker.",
        server_plugins=flow.server_plugin,
    )
    bind_flow_commands(app, flow)
    if flow.cli_register:
        flow.cli_register(app, flow)
    app()
