"""Knowledge-graph capture — projecting a flow into a curatable graph on disk.

webwalk already models a flow as *states* (vertices) + *rules* (edges). This
package projects that model into the on-disk format the sibling
`knowledge-graph-editor` (kge) reads — `schema.json` / `nodes.json` /
`edges.json` — so the **acquisition graph** ("where to find it") can be browsed
and extended with **semantic meaning** ("what it is") in kge's UI.

Two families of node/edge live in one graph, told apart by kge's `family` field:

  * ``acquisition`` — machine-authored, regenerated on every ``graph`` run: a
    ``page`` node per state signature and a ``NAVIGATES_TO``/``DISCOVERS`` edge
    per rule→destination.
  * ``semantic`` — human/AI-authored in kge, **preserved** across regenerations.

The clobber is scoped by node/edge *type*: regenerating rewrites only the
generated types and leaves every other type untouched — so re-walking a flow
never destroys curated knowledge (the discipline kge itself uses to keep
``views/`` separate from ``nodes``/``edges``).
"""

from __future__ import annotations

from webwalk.graph.kge import states_to_graph, write_kge_graph
from webwalk.graph.model import model_to_graph

__all__ = ["states_to_graph", "model_to_graph", "write_kge_graph"]
