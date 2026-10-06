"""The network graph: nodes, two-port components, and topology validation.

Deliberately not built on networkx. What the solver needs is adjacency plus
signed mass-flow residuals per node, which is a few dozen lines here; a graph
library would add a dependency without removing any of the domain-specific
work.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum

from .components import FlowComponent, Node, NodeKind, SinkSpec


class Severity(str, Enum):
    ERROR = "error"
    """The network cannot be solved as drawn."""

    WARNING = "warning"
    """Solvable, but probably not what was intended."""

    INFO = "info"


@dataclass
class ValidationIssue:
    severity: Severity
    message: str
    node_ids: tuple[str, ...] = ()
    component_ids: tuple[str, ...] = ()

    def __str__(self) -> str:
        return f"[{self.severity.value}] {self.message}"


class NetworkError(ValueError):
    """Raised when a network is asked to solve despite ERROR-level issues."""


@dataclass
class Network:
    """A feed system: nodes joined by two-port flow components."""

    nodes: dict[str, Node] = field(default_factory=dict)
    components: dict[str, FlowComponent] = field(default_factory=dict)
    name: str = "Untitled"

    # --- construction ------------------------------------------------------

    def add_node(self, node: Node) -> Node:
        if node.id in self.nodes:
            raise ValueError(f"Duplicate node id {node.id!r}")
        self.nodes[node.id] = node
        return node

    def add_component(self, component: FlowComponent) -> FlowComponent:
        if component.id in self.components:
            raise ValueError(f"Duplicate component id {component.id!r}")
        for ref in (component.from_node, component.to_node):
            if ref not in self.nodes:
                raise ValueError(
                    f"Component {component.name!r} references unknown node {ref!r}"
                )
        if component.from_node == component.to_node:
            raise ValueError(
                f"Component {component.name!r} connects node {component.from_node!r} "
                "to itself"
            )
        self.components[component.id] = component
        return component

    def remove_component(self, component_id: str) -> None:
        self.components.pop(component_id, None)

    def remove_node(self, node_id: str) -> None:
        """Remove a node and every component attached to it."""
        self.nodes.pop(node_id, None)
        for cid in [
            cid
            for cid, c in self.components.items()
            if node_id in (c.from_node, c.to_node)
        ]:
            del self.components[cid]

    # --- queries -----------------------------------------------------------

    def components_at(self, node_id: str) -> list[FlowComponent]:
        return [
            c
            for c in self.components.values()
            if node_id in (c.from_node, c.to_node)
        ]

    def neighbors(self, node_id: str) -> set[str]:
        out: set[str] = set()
        for c in self.components_at(node_id):
            out.add(c.to_node if c.from_node == node_id else c.from_node)
        return out

    def nodes_of_kind(self, kind: NodeKind) -> list[Node]:
        return [n for n in self.nodes.values() if n.kind is kind]

    @property
    def source_nodes(self) -> list[Node]:
        return self.nodes_of_kind(NodeKind.SOURCE)

    @property
    def sink_nodes(self) -> list[Node]:
        return self.nodes_of_kind(NodeKind.SINK)

    @property
    def sink(self) -> Node:
        sinks = self.sink_nodes
        if len(sinks) != 1:
            raise NetworkError(
                f"Exactly one engine/sink is required; found {len(sinks)}"
            )
        return sinks[0]

    @property
    def dynamic_nodes(self) -> list[Node]:
        """Nodes carrying gas mass that evolves during a transient run."""
        return [n for n in self.nodes.values() if n.is_dynamic]

    def connected_component(self, start_id: str) -> set[str]:
        seen = {start_id}
        queue = deque([start_id])
        while queue:
            current = queue.popleft()
            for nbr in self.neighbors(current):
                if nbr not in seen:
                    seen.add(nbr)
                    queue.append(nbr)
        return seen

    def independent_loop_count(self, start_id: str) -> int:
        """Number of independent cycles, via the cyclomatic number.

        ``edges - nodes + 1`` over the connected subgraph reachable from
        ``start_id``. Zero means a pure tree (series and branching only);
        each additional loop is one place where branches merge back together.
        """
        reachable = self.connected_component(start_id)
        edges = sum(
            1
            for c in self.components.values()
            if c.from_node in reachable and c.to_node in reachable
        )
        return edges - len(reachable) + 1

    # --- validation --------------------------------------------------------

    def validate(self) -> list[ValidationIssue]:
        """Check the network against the v1 scope.

        Note on loops: a branch that splits and merges back creates a cycle in
        the undirected graph. That is a *parallel path*, not a flow loop, and
        the nodal solver handles it correctly, so it is reported as INFO rather
        than rejected. A genuine recirculating loop would need a compressor,
        which v1 does not model.
        """
        issues: list[ValidationIssue] = []

        if not self.nodes:
            issues.append(ValidationIssue(Severity.ERROR, "Network is empty"))
            return issues

        sinks = self.sink_nodes
        if not sinks:
            issues.append(
                ValidationIssue(Severity.ERROR, "No engine/sink node defined")
            )
        elif len(sinks) > 1:
            issues.append(
                ValidationIssue(
                    Severity.ERROR,
                    f"v1 supports exactly one engine/sink; found {len(sinks)}",
                    node_ids=tuple(n.id for n in sinks),
                )
            )

        sources = self.source_nodes
        if not sources:
            issues.append(ValidationIssue(Severity.ERROR, "No source/tank node defined"))

        # Dangling nodes and unreachable islands
        for node in self.nodes.values():
            attached = self.components_at(node.id)
            if not attached:
                issues.append(
                    ValidationIssue(
                        Severity.ERROR,
                        f"{node.name!r} is not connected to anything",
                        node_ids=(node.id,),
                    )
                )
            elif node.kind is NodeKind.SINK and len(attached) > 1:
                issues.append(
                    ValidationIssue(
                        Severity.WARNING,
                        f"Engine {node.name!r} is fed by {len(attached)} components; "
                        "these are treated as parallel feeds into one chamber",
                        node_ids=(node.id,),
                    )
                )

        if sources and sinks:
            reachable = self.connected_component(sources[0].id)
            if sinks[0].id not in reachable:
                issues.append(
                    ValidationIssue(
                        Severity.ERROR,
                        f"No flow path from {sources[0].name!r} to {sinks[0].name!r}",
                        node_ids=(sources[0].id, sinks[0].id),
                    )
                )
            stranded = set(self.nodes) - reachable
            if stranded:
                names = ", ".join(sorted(self.nodes[n].name for n in stranded))
                issues.append(
                    ValidationIssue(
                        Severity.ERROR,
                        f"Not connected to the source train: {names}",
                        node_ids=tuple(sorted(stranded)),
                    )
                )
            else:
                loops = self.independent_loop_count(sources[0].id)
                if loops > 0:
                    issues.append(
                        ValidationIssue(
                            Severity.INFO,
                            f"{loops} parallel flow path(s): branches that merge "
                            "back together. The nodal solver handles these.",
                        )
                    )

        # Per-node and per-component sanity
        for node in self.nodes.values():
            issues.extend(self._validate_node(node))
        for comp in self.components.values():
            if comp.cv <= 0.0:
                issues.append(
                    ValidationIssue(
                        Severity.WARNING,
                        f"{comp.name!r} has Cv <= 0 and blocks all flow",
                        component_ids=(comp.id,),
                    )
                )

        return issues

    def _validate_node(self, node: Node) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []

        if node.kind is NodeKind.SOURCE:
            if node.supply_pressure_psig is None and node.charge_pressure_psig is None:
                issues.append(
                    ValidationIssue(
                        Severity.ERROR,
                        f"Source {node.name!r} has no pressure set",
                        node_ids=(node.id,),
                    )
                )
            if node.source_mode.value == "finite_volume" and not node.volume_m3:
                issues.append(
                    ValidationIssue(
                        Severity.ERROR,
                        f"Source {node.name!r} is finite-volume but has no volume",
                        node_ids=(node.id,),
                    )
                )

        if node.kind is NodeKind.ACCUMULATOR:
            if not node.volume_m3:
                issues.append(
                    ValidationIssue(
                        Severity.ERROR,
                        f"Accumulator {node.name!r} has no volume",
                        node_ids=(node.id,),
                    )
                )
            if node.charge_pressure_psig is None:
                issues.append(
                    ValidationIssue(
                        Severity.ERROR,
                        f"Accumulator {node.name!r} has no initial charge pressure",
                        node_ids=(node.id,),
                    )
                )

        if node.kind is NodeKind.SINK:
            needs_mdot = node.sink_spec in (SinkSpec.MASS_FLOW, SinkSpec.BOTH)
            needs_p = node.sink_spec in (SinkSpec.PRESSURE, SinkSpec.BOTH)
            if needs_mdot and node.target_mdot_kgs is None:
                issues.append(
                    ValidationIssue(
                        Severity.ERROR,
                        f"Engine {node.name!r} needs a target mass flow",
                        node_ids=(node.id,),
                    )
                )
            if needs_p and node.target_pressure_psig is None:
                issues.append(
                    ValidationIssue(
                        Severity.ERROR,
                        f"Engine {node.name!r} needs a target pressure",
                        node_ids=(node.id,),
                    )
                )

        return issues

    def raise_on_errors(self) -> list[ValidationIssue]:
        """Validate, raising if anything is ERROR level. Returns the rest."""
        issues = self.validate()
        errors = [i for i in issues if i.severity is Severity.ERROR]
        if errors:
            raise NetworkError(
                "Network cannot be solved:\n  "
                + "\n  ".join(e.message for e in errors)
            )
        return issues
