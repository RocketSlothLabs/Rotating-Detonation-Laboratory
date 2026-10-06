"""The P&ID canvas: a QGraphicsScene over the model, plus its view.

Interaction:

* drag a node type from the palette onto the canvas to create it;
* drag from a node's right-hand port onto another node to connect them with
  whatever component is selected in the palette (a plain pipe if none);
* click to select and edit in the property dock; Delete removes the selection;
* wheel zooms, middle-drag pans.

Every edit mutates the model objects directly, so the canvas never holds state
the solver cannot see.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QPainter, QPen
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsScene,
    QGraphicsView,
)

from ..model.components import (
    ComponentType,
    FlowComponent,
    Node,
    NodeKind,
    SinkSpec,
)
from ..model.library import LibraryPart
from ..model.network import Network
from ..units import LITER_TO_M3
from . import style
from .items import EdgeItem, NodeItem, PortItem, assign_ports_for_node

MIME_NODE = "application/x-pidsim-node"
MIME_PART = "application/x-pidsim-part"


def default_node(kind: NodeKind) -> Node:
    """A new node of ``kind`` with sensible starting values."""
    node = Node(kind=kind)
    if kind is NodeKind.SOURCE:
        node.name = "Supply"
        node.supply_pressure_psig = 500.0
    elif kind is NodeKind.ACCUMULATOR:
        node.name = "Accumulator"
        node.volume_m3 = 10.0 * LITER_TO_M3
        node.charge_pressure_psig = 500.0
    elif kind is NodeKind.SINK:
        node.name = "Engine"
        node.sink_spec = SinkSpec.BOTH
        node.target_pressure_psig = 150.0
        node.target_mdot_kgs = 0.110
    elif kind is NodeKind.TEE:
        node.name = "Tee"
    else:
        node.name = "Junction"
    return node


class PidScene(QGraphicsScene):
    """Holds the canvas items and keeps them in step with the model."""

    selection_changed = Signal(object)  # Node | FlowComponent | None
    network_changed = Signal()
    status_message = Signal(str)

    def __init__(self, network: Network, parent=None):
        super().__init__(parent)
        self.network = network
        self.node_items: dict[str, NodeItem] = {}
        self.edge_items: dict[str, EdgeItem] = {}
        self.pending_part: LibraryPart | None = None

        self._drag_line: QGraphicsLineItem | None = None
        self._drag_from: str | None = None

        self.setBackgroundBrush(QBrush(style.BACKGROUND))
        self.setSceneRect(-2000, -2000, 4000, 4000)
        self.selectionChanged.connect(self._on_selection_changed)

    # --- population --------------------------------------------------------

    def rebuild(self, network: Network) -> None:
        """Discard every item and rebuild from ``network``."""
        self.network = network
        self.clear()
        self.node_items.clear()
        self.edge_items.clear()
        self._drag_line = None
        self._drag_from = None

        for node in network.nodes.values():
            item = NodeItem(node)
            self.addItem(item)
            self.node_items[node.id] = item
        for comp in network.components.values():
            self._add_edge_item(comp)
        self.reassign_all_ports()
        self.network_changed.emit()

    def reassign_all_ports(self) -> None:
        """Settle every line onto a leg.

        Two passes: the first works from node centres, the second refines using
        the anchors the first chose, which is what stops parallel lines between
        the same pair of nodes from crossing.
        """
        for _ in range(2):
            for item in self.node_items.values():
                assign_ports_for_node(item)
        self._stagger_parallel_labels()

    def _stagger_parallel_labels(self) -> None:
        """Slide labels along the line when several lines join the same pair."""
        groups: dict[frozenset[str], list[EdgeItem]] = {}
        for edge in self.edge_items.values():
            key = frozenset((edge.component.from_node, edge.component.to_node))
            groups.setdefault(key, []).append(edge)

        for members in groups.values():
            count = len(members)
            for i, edge in enumerate(members):
                edge.label_shift = (
                    0.0 if count == 1 else (i - (count - 1) / 2) * (0.7 / count)
                )
                edge.update()

    def _add_edge_item(self, comp: FlowComponent) -> EdgeItem | None:
        src = self.node_items.get(comp.from_node)
        dst = self.node_items.get(comp.to_node)
        if src is None or dst is None:
            return None
        edge = EdgeItem(comp, src, dst)
        self.addItem(edge)
        self.edge_items[comp.id] = edge
        return edge

    def add_node(self, kind: NodeKind, scene_pos: QPointF) -> NodeItem:
        node = default_node(kind)
        node.x, node.y = scene_pos.x(), scene_pos.y()
        self.network.add_node(node)
        item = NodeItem(node)
        self.addItem(item)
        self.node_items[node.id] = item
        self.network_changed.emit()
        return item

    def connect_nodes(self, from_id: str, to_id: str) -> EdgeItem | None:
        if from_id == to_id:
            self.status_message.emit("A component cannot connect a node to itself")
            return None

        if self.pending_part is not None:
            comp = self.pending_part.instantiate(from_id, to_id)
        else:
            comp = FlowComponent(
                type=ComponentType.PIPE,
                from_node=from_id,
                to_node=to_id,
                cv=10.8,
                name="Line",
            )
        try:
            self.network.add_component(comp)
        except ValueError as exc:
            self.status_message.emit(str(exc))
            return None

        edge = self._add_edge_item(comp)
        self.reassign_all_ports()
        self.network_changed.emit()
        self.status_message.emit(
            f"Connected {self.network.nodes[from_id].name} -> "
            f"{self.network.nodes[to_id].name} with {comp.name}"
        )
        return edge

    def rotate_selection(self, delta: float = 90.0) -> None:
        """Turn the selected nodes. Presentation only -- results are untouched."""
        rotated = [
            item for item in self.selectedItems() if isinstance(item, NodeItem)
        ]
        if not rotated:
            self.status_message.emit("Select a component or node to rotate")
            return
        for item in rotated:
            item.rotate_by(delta)
        self.reassign_all_ports()
        if len(rotated) == 1:
            self.status_message.emit(
                f"{rotated[0].node.name} rotated to {rotated[0].rotation_deg:g}"
                "\N{DEGREE SIGN}"
            )
        else:
            self.status_message.emit(f"Rotated {len(rotated)} nodes")

    def delete_selection(self) -> None:
        removed = False
        for item in list(self.selectedItems()):
            if isinstance(item, EdgeItem):
                self.network.remove_component(item.component_id)
                item.detach()
                self.removeItem(item)
                self.edge_items.pop(item.component_id, None)
                removed = True
            elif isinstance(item, NodeItem):
                for comp in self.network.components_at(item.node_id):
                    edge = self.edge_items.pop(comp.id, None)
                    if edge is not None:
                        edge.detach()
                        self.removeItem(edge)
                self.network.remove_node(item.node_id)
                self.removeItem(item)
                self.node_items.pop(item.node_id, None)
                removed = True
        if removed:
            self.selection_changed.emit(None)
            self.network_changed.emit()

    # --- results -----------------------------------------------------------

    def clear_results(self) -> None:
        for item in self.node_items.values():
            item.set_result([])
        for edge in self.edge_items.values():
            edge.set_result("", False)

    def show_steady_result(self, result) -> None:
        from ..units import psia_to_psig

        for node_id, item in self.node_items.items():
            p = result.node_pressures_psia.get(node_id)
            item.set_result(
                [f"{psia_to_psig(p):.1f} psig", f"{p:.1f} psia"] if p else []
            )
        for comp_id, edge in self.edge_items.items():
            flow = result.component_flows.get(comp_id)
            if flow is None:
                edge.set_result("", False)
                continue
            choked = flow.regime.value == "choked"
            text = f"{flow.mdot_gs:.1f} g/s"
            if choked:
                text += "  (choked)"
            edge.set_result(text, choked)

    def show_transient_snapshot(self, result, t: float) -> None:
        from ..units import psia_to_psig

        snap = result.at(t)
        for node_id, item in self.node_items.items():
            p = snap["node_pressure_psia"].get(node_id)
            temp = snap["node_temperature_K"].get(node_id)
            if p is None:
                item.set_result([])
                continue
            lines = [f"{psia_to_psig(p):.1f} psig"]
            if temp is not None:
                lines.append(f"{temp:.1f} K")
            item.set_result(lines)
        for comp_id, edge in self.edge_items.items():
            mdot = snap["component_mdot_kgs"].get(comp_id)
            edge.set_result(f"{mdot * 1e3:.1f} g/s" if mdot is not None else "", False)

    def refresh_item(self, obj_id: str) -> None:
        if obj_id in self.node_items:
            self.node_items[obj_id].refresh()
            for edge in self.edge_items.values():
                edge.refresh_geometry()
        elif obj_id in self.edge_items:
            self.edge_items[obj_id].update()

    # --- interaction -------------------------------------------------------

    def _on_selection_changed(self) -> None:
        items = self.selectedItems()
        if not items:
            self.selection_changed.emit(None)
            return
        item = items[0]
        if isinstance(item, NodeItem):
            self.selection_changed.emit(item.node)
        elif isinstance(item, EdgeItem):
            self.selection_changed.emit(item.component)
        else:
            self.selection_changed.emit(None)

    def _port_at(self, pos: QPointF) -> PortItem | None:
        for item in self.items(pos):
            if isinstance(item, PortItem) and item.isVisible():
                return item
        return None

    def _node_at(self, pos: QPointF) -> NodeItem | None:
        for item in self.items(pos):
            if isinstance(item, PortItem):
                return item.node_item
            if isinstance(item, NodeItem):
                return item
        return None

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            port = self._port_at(event.scenePos())
            if port is not None and port.can_start_connection:
                self._drag_from = port.node_id
                self._drag_line = QGraphicsLineItem()
                pen = QPen(style.LINE_HIGHLIGHT, 1.6, Qt.PenStyle.DashLine)
                self._drag_line.setPen(pen)
                self._drag_line.setZValue(10)
                start = port.scenePos()
                self._drag_line.setLine(
                    start.x(), start.y(), event.scenePos().x(), event.scenePos().y()
                )
                self.addItem(self._drag_line)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_line is not None:
            line = self._drag_line.line()
            self._drag_line.setLine(
                line.x1(), line.y1(), event.scenePos().x(), event.scenePos().y()
            )
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag_line is not None:
            self.removeItem(self._drag_line)
            self._drag_line = None
            target = self._node_at(event.scenePos())
            source_id, self._drag_from = self._drag_from, None
            if target is not None and source_id is not None:
                if target.node.kind is NodeKind.SOURCE:
                    self.status_message.emit(
                        "A source has no inlet -- connect into something else"
                    )
                else:
                    self.connect_nodes(source_id, target.node_id)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:
        super().drawBackground(painter, rect)
        step = style.GRID
        left = int(rect.left()) - (int(rect.left()) % int(step))
        top = int(rect.top()) - (int(rect.top()) % int(step))

        minor = QPen(style.GRID_MINOR, 1.0)
        major = QPen(style.GRID_MAJOR, 1.0)
        x = left
        while x < rect.right():
            painter.setPen(major if int(x) % int(step * 5) == 0 else minor)
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            x += step
        y = top
        while y < rect.bottom():
            painter.setPen(major if int(y) % int(step * 5) == 0 else minor)
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            y += step


class PidView(QGraphicsView):
    """Zoom, pan and drop handling for the canvas."""

    def __init__(self, scene: PidScene, parent=None):
        super().__init__(scene, parent)
        self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setAcceptDrops(True)
        self.setTransformationAnchor(
            QGraphicsView.ViewportAnchor.AnchorUnderMouse
        )
        self._zoom = 1.0

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        new_zoom = self._zoom * factor
        if 0.15 <= new_zoom <= 6.0:
            self._zoom = new_zoom
            self.scale(factor, factor)

    def reset_zoom(self) -> None:
        self.resetTransform()
        self._zoom = 1.0

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.MiddleButton:
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            fake = event
            super().mousePressEvent(fake)
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.MiddleButton:
            self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)

    # --- drag and drop from the palette ------------------------------------

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(MIME_NODE) or event.mimeData().hasFormat(
            MIME_PART
        ):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasFormat(MIME_NODE) or event.mimeData().hasFormat(
            MIME_PART
        ):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        data = event.mimeData()
        pos = self.mapToScene(event.position().toPoint())
        scene: PidScene = self.scene()

        if data.hasFormat(MIME_NODE):
            kind = NodeKind(bytes(data.data(MIME_NODE)).decode())
            item = scene.add_node(kind, pos)
            scene.clearSelection()
            item.setSelected(True)
            event.acceptProposedAction()
        elif data.hasFormat(MIME_PART):
            scene.status_message.emit(
                "Component selected. Drag from one node's right-hand port to "
                "another node to place it."
            )
            event.acceptProposedAction()
        else:
            super().dropEvent(event)
