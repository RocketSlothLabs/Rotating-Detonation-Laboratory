"""Canvas items: nodes, their ports, and the components that join them.

Mirrors the model's split (see :mod:`pidsim.model.components`): a node is a box
on the canvas, a component is drawn as a symbol sitting on the line between two
nodes. Items hold only ids and presentation state -- the model objects remain
the single source of truth, and every edit here writes straight through to them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsObject,
)

from ..model.components import ComponentType, FlowComponent, Node, NodeKind
from . import style


def _label_font(size: int = 8, bold: bool = False) -> QFont:
    font = QFont()
    font.setPointSize(size)
    font.setBold(bold)
    return font


def rotate_point(point: QPointF, degrees: float) -> QPointF:
    """Rotate about the origin, clockwise on screen (Qt's y axis points down)."""
    if not degrees:
        return point
    rad = math.radians(degrees)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    return QPointF(
        point.x() * cos_a - point.y() * sin_a,
        point.x() * sin_a + point.y() * cos_a,
    )


@dataclass(frozen=True)
class PortSpec:
    """One connection anchor, positioned as a fraction of the node's half-size.

    ``role`` is "in", "out" or "both". A tee's legs are all "both" because a
    tee is genuinely bidirectional -- which leg is the inlet depends on how it
    is plumbed, not on the fitting.
    """

    name: str
    role: str
    fx: float
    fy: float

    def offset(self) -> QPointF:
        return QPointF(self.fx * style.NODE_W / 2, self.fy * style.NODE_H / 2)

    def direction(self) -> QPointF:
        length = math.hypot(self.fx, self.fy) or 1.0
        return QPointF(self.fx / length, self.fy / length)


# How many anchors each kind of node shows, and where.
#
# A tee gets three legs (in, out, branch) and the engine gets three inlets,
# because an RDE chamber is fed by several branches at once. The model has
# always allowed any number of components per node -- these are the visual
# anchors, and edges are distributed across them by direction.
_TWO_PORT = (
    PortSpec("in", "in", -1.0, 0.0),
    PortSpec("out", "out", 1.0, 0.0),
)

PORT_LAYOUTS: dict[NodeKind, tuple[PortSpec, ...]] = {
    NodeKind.SOURCE: (PortSpec("out", "out", 1.0, 0.0),),
    NodeKind.JUNCTION: _TWO_PORT,
    NodeKind.ACCUMULATOR: _TWO_PORT,
    NodeKind.TEE: (
        PortSpec("west", "both", -1.0, 0.0),
        PortSpec("east", "both", 1.0, 0.0),
        PortSpec("branch", "both", 0.0, 1.0),
    ),
    NodeKind.SINK: (
        PortSpec("upper", "in", -1.0, -0.55),
        PortSpec("middle", "in", -1.0, 0.0),
        PortSpec("lower", "in", -1.0, 0.55),
    ),
}

RESULT_W = 120.0
"""Width reserved beside a quarter-turned node for its result annotation."""

LABEL_W = 170.0
"""Width of a line's name / result label box."""

_TOOLTIPS = {
    "in": "Inlet -- drag another component's port here",
    "out": "Outlet -- drag from here to another component",
    "both": "Tee leg -- drag from here, or drop a connection onto it",
}


class PortItem(QGraphicsEllipseItem):
    """A connection anchor on a node. Dragging from one starts a component."""

    def __init__(self, parent: "NodeItem", spec: PortSpec):
        r = style.PORT_R
        super().__init__(-r, -r, 2 * r, 2 * r, parent)
        self.spec = spec
        self.role = spec.role
        self.name = spec.name
        self.node_item = parent
        self.setPos(rotate_point(spec.offset(), parent.rotation_deg))
        self.setPen(QPen(style.OUTLINE, 1.0))
        self.setBrush(QBrush(style.PORT_IDLE))
        self.setAcceptHoverEvents(True)
        self.setZValue(3)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setToolTip(_TOOLTIPS.get(spec.role, ""))

    @property
    def node_id(self) -> str:
        return self.node_item.node_id

    @property
    def can_start_connection(self) -> bool:
        return self.role in ("out", "both")

    def direction(self) -> QPointF:
        """Which way this leg points, accounting for the node's rotation."""
        return rotate_point(self.spec.direction(), self.node_item.rotation_deg)

    def accepts(self, outgoing: bool) -> bool:
        """Can an edge leaving (``outgoing``) or entering this node use it?"""
        if self.role == "both":
            return True
        return self.role == ("out" if outgoing else "in")

    def hoverEnterEvent(self, event):
        self.setBrush(QBrush(style.PORT_HOVER))
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self.setBrush(QBrush(style.PORT_IDLE))
        super().hoverLeaveEvent(event)

    def highlight(self, on: bool, valid: bool = True) -> None:
        if on:
            self.setBrush(QBrush(style.PORT_VALID if valid else style.PORT_HOVER))
        else:
            self.setBrush(QBrush(style.PORT_IDLE))


class NodeItem(QGraphicsObject):
    """A junction, tee, source, accumulator or engine on the canvas."""

    def __init__(self, node: Node):
        super().__init__()
        self.node_id = node.id
        self.node = node
        self.result_lines: list[str] = []
        self._edges: list["EdgeItem"] = []

        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setPos(node.x, node.y)
        self.setZValue(2)

        self.ports: list[PortItem] = []
        self._layout_kind: NodeKind | None = None
        self._build_ports()

    # --- rotation ----------------------------------------------------------

    @property
    def rotation_deg(self) -> float:
        """Node rotation in degrees. Named to avoid shadowing QGraphicsItem.rotation().

        The item itself is never rotated with ``setRotation`` -- that would turn
        the labels on their side too. Only the glyph and the leg positions turn;
        text stays upright and readable at every angle.
        """
        return float(getattr(self.node, "rotation", 0.0) or 0.0)

    def _apply_rotation(self) -> None:
        for port in self.ports:
            port.setPos(rotate_point(port.spec.offset(), self.rotation_deg))

    def _quarter_turned(self) -> bool:
        return int(round(self.rotation_deg / 90.0)) % 2 == 1

    def _body_size(self) -> tuple[float, float]:
        """On-screen footprint, which swaps at 90 and 270 degrees."""
        if self._quarter_turned():
            return style.NODE_H, style.NODE_W
        return style.NODE_W, style.NODE_H

    def rotate_by(self, delta: float) -> None:
        """Turn the node and re-seat every line that meets it."""
        self.prepareGeometryChange()
        self.node.rotation = (self.rotation_deg + delta) % 360.0
        self._apply_rotation()
        touched = {self}
        for edge in self._edges:
            touched.add(edge.src)
            touched.add(edge.dst)
        for item in touched:
            assign_ports_for_node(item)
        self.update()

    # --- ports -------------------------------------------------------------

    def _build_ports(self) -> None:
        for port in self.ports:
            port.setParentItem(None)
            scene = port.scene()
            if scene is not None:
                scene.removeItem(port)
        layout = PORT_LAYOUTS.get(self.node.kind, _TWO_PORT)
        self.ports = [PortItem(self, spec) for spec in layout]
        self._layout_kind = self.node.kind

    @property
    def inlet_ports(self) -> list[PortItem]:
        return [p for p in self.ports if p.accepts(outgoing=False)]

    @property
    def outlet_ports(self) -> list[PortItem]:
        return [p for p in self.ports if p.accepts(outgoing=True)]

    @property
    def port_in(self) -> PortItem | None:
        """First inlet anchor, or None for a source."""
        ports = self.inlet_ports
        return ports[0] if ports else None

    @property
    def port_out(self) -> PortItem | None:
        """First outlet anchor, or None for an engine."""
        ports = self.outlet_ports
        return ports[0] if ports else None

    # --- geometry ----------------------------------------------------------

    def boundingRect(self) -> QRectF:
        w, h = self._body_size()
        if self._quarter_turned():
            # Turned nodes stack vertically, so results go beside them and the
            # vertical corridor stays clear for the pipe runs.
            return QRectF(-w / 2 - 6, -h / 2 - 6, w + 12 + RESULT_W, h + 12)
        return QRectF(-w / 2 - 6, -h / 2 - 6, w + 12, h + 68)

    def shape(self) -> QPainterPath:
        """Click area is the body only, never the label margin around it."""
        w, h = self._body_size()
        path = QPainterPath()
        path.addRect(QRectF(-w / 2, -h / 2, w, h))
        return path

    def register_edge(self, edge: "EdgeItem") -> None:
        if edge not in self._edges:
            self._edges.append(edge)

    def unregister_edge(self, edge: "EdgeItem") -> None:
        if edge in self._edges:
            self._edges.remove(edge)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self.node.x = self.pos().x()
            self.node.y = self.pos().y()
            # Moving a node can change which leg each line should attach to,
            # on this node and on everything it touches.
            touched = {self}
            for edge in self._edges:
                touched.add(edge.src)
                touched.add(edge.dst)
            for item in touched:
                assign_ports_for_node(item)
        return super().itemChange(change, value)

    def refresh(self) -> None:
        self.prepareGeometryChange()
        if self.node.kind is not self._layout_kind:
            self._build_ports()
            for edge in self._edges:
                edge.src_port = None
                edge.dst_port = None
        self._apply_rotation()
        touched = {self}
        for edge in self._edges:
            touched.add(edge.src)
            touched.add(edge.dst)
        for item in touched:
            assign_ports_for_node(item)
        self.update()

    def set_result(self, lines: list[str]) -> None:
        self.result_lines = lines
        self.update()

    # --- painting ----------------------------------------------------------

    def paint(self, painter: QPainter, option, widget=None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w, h = style.NODE_W, style.NODE_H
        body = QRectF(-w / 2, -h / 2, w, h)

        fill = style.FILL_SELECTED if self.isSelected() else style.FILL
        # Only the glyph turns. Labels are drawn afterwards, unrotated, so they
        # stay horizontal and readable whichever way the node faces.
        painter.save()
        painter.rotate(self.rotation_deg)
        painter.setPen(QPen(style.OUTLINE, 2.0 if self.isSelected() else 1.4))
        painter.setBrush(QBrush(fill))
        self._paint_symbol(painter, body)
        painter.restore()

        bw, bh = self._body_size()
        painter.setPen(QPen(style.TEXT))
        painter.setFont(_label_font(7, bold=True))
        # AlignmentFlag and TextFlag are separate enums in PySide6; OR-ing them
        # directly does not produce a usable flag value, so combine the ints.
        flags = (
            int(Qt.AlignmentFlag.AlignCenter.value)
            | int(Qt.TextFlag.TextWordWrap.value)
        )
        # Inset away from whichever edges carry the leg stubs.
        inset_x, inset_y = (4, 13) if self._quarter_turned() else (13, 4)
        painter.drawText(
            QRectF(
                -bw / 2 + inset_x,
                -bh / 2 + inset_y,
                bw - 2 * inset_x,
                bh - 2 * inset_y,
            ),
            flags,
            self.node.name,
        )

        if self.result_lines:
            painter.setPen(QPen(style.RESULT_TEXT))
            painter.setFont(_label_font(8))
            text = "\n".join(self.result_lines)
            if self._quarter_turned():
                painter.drawText(
                    QRectF(bw / 2 + 8, -23, RESULT_W - 14, 46),
                    int(
                        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                    ),
                    text,
                )
            else:
                # Clear of any leg that sits on the bottom edge.
                painter.drawText(
                    QRectF(-bw / 2 - 20, bh / 2 + 12, bw + 40, 46),
                    int(
                        Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop
                    ),
                    text,
                )

    def _paint_symbol(self, painter: QPainter, body: QRectF) -> None:
        kind = self.node.kind
        if kind is NodeKind.SOURCE:
            painter.drawRoundedRect(body, 14, 14)
            painter.drawLine(
                QPointF(body.left() + 10, body.top() + 6),
                QPointF(body.left() + 10, body.bottom() - 6),
            )
        elif kind is NodeKind.ACCUMULATOR:
            painter.drawRoundedRect(body, body.height() / 2, body.height() / 2)
        elif kind is NodeKind.SINK:
            path = QPainterPath()
            path.moveTo(body.left(), body.top())
            path.lineTo(body.right() - 14, body.top() + 10)
            path.lineTo(body.right() - 14, body.bottom() - 10)
            path.lineTo(body.left(), body.bottom())
            path.closeSubpath()
            painter.drawPath(path)
            painter.drawRect(
                QRectF(body.right() - 14, body.top() + 10, 14, body.height() - 20)
            )
        elif kind in (NodeKind.TEE, NodeKind.JUNCTION):
            painter.drawRoundedRect(body, 4, 4)
            painter.setPen(QPen(style.OUTLINE, 2.0))
            # Short stubs from each port inwards rather than lines across the
            # whole body, so the glyph reads as a tee without striking through
            # the node's name.
            stub = 13.0
            painter.drawLine(QPointF(body.left(), 0), QPointF(body.left() + stub, 0))
            painter.drawLine(QPointF(body.right() - stub, 0), QPointF(body.right(), 0))
            if self.node.kind is NodeKind.TEE:
                painter.drawLine(
                    QPointF(0, body.bottom() - stub), QPointF(0, body.bottom())
                )
        else:
            painter.drawRect(body)


def _label_box(
    anchor: QPointF, perp: QPointF, distance: float, upright: bool
) -> tuple[QRectF, int]:
    """Place a label ``distance`` along ``perp`` from ``anchor``.

    For a horizontal run the label is centred above or below the line. For a
    vertical one it is pushed clear to the left or right and aligned away from
    the pipe, so the text never sits on top of it.
    """
    point = QPointF(
        anchor.x() + perp.x() * distance, anchor.y() + perp.y() * distance
    )
    if upright:
        return (
            QRectF(point.x() - LABEL_W / 2, point.y() - 9, LABEL_W, 18),
            int(Qt.AlignmentFlag.AlignCenter),
        )
    if perp.x() * distance > 0:  # label sits to the right of the line
        return (
            QRectF(point.x(), point.y() - 9, LABEL_W, 18),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
        )
    return (
        QRectF(point.x() - LABEL_W, point.y() - 9, LABEL_W, 18),
        int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
    )


class EdgeItem(QGraphicsObject):
    """A flow component, drawn as a symbol on the line between two nodes."""

    def __init__(self, component: FlowComponent, src: NodeItem, dst: NodeItem):
        super().__init__()
        self.component_id = component.id
        self.component = component
        self.src = src
        self.dst = dst
        self.result_text = ""
        self.choked = False

        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.setZValue(1)
        self.setAcceptHoverEvents(True)

        self.src_port: PortItem | None = None
        self.dst_port: PortItem | None = None
        self.label_shift = 0.0
        """Fraction along the line to place labels, relative to the midpoint.

        Lines running between the same pair of nodes are staggered so their
        labels do not land on top of one another.
        """

        src.register_edge(self)
        dst.register_edge(self)
        self._p1 = QPointF()
        self._p2 = QPointF()
        assign_ports_for_node(src)
        assign_ports_for_node(dst)
        self.refresh_geometry()

    def detach(self) -> None:
        self.src.unregister_edge(self)
        self.dst.unregister_edge(self)
        assign_ports_for_node(self.src)
        assign_ports_for_node(self.dst)

    def set_port(self, node_item: "NodeItem", port: PortItem) -> None:
        if node_item is self.src:
            self.src_port = port
        else:
            self.dst_port = port

    # --- geometry ----------------------------------------------------------

    def _anchor(self, node_item: "NodeItem", port: PortItem | None) -> QPointF:
        if port is None:
            ports = node_item.outlet_ports if node_item is self.src else (
                node_item.inlet_ports
            )
            port = ports[0] if ports else (node_item.ports[0] if node_item.ports else None)
        if port is None:
            return node_item.scenePos()
        return node_item.mapToScene(port.pos())

    def refresh_geometry(self) -> None:
        self.prepareGeometryChange()
        self._p1 = self._anchor(self.src, self.src_port)
        self._p2 = self._anchor(self.dst, self.dst_port)
        self.update()

    def boundingRect(self) -> QRectF:
        rect = QRectF(self._p1, self._p2).normalized()
        # Wide enough for labels pushed out to either side of a vertical run.
        return rect.adjusted(-LABEL_W - 30, -40, LABEL_W + 30, 40)

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        stroker_width = 12.0
        mid = (self._p1 + self._p2) / 2
        path.addEllipse(mid, stroker_width * 2, stroker_width * 2)
        path.moveTo(self._p1)
        path.lineTo(self._p2)
        return path

    # --- painting ----------------------------------------------------------

    def paint(self, painter: QPainter, option, widget=None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        if self.component.effective_cv() <= 0.0:
            colour = style.LINE_SHUT
        elif self.choked:
            colour = style.LINE_CHOKED
        else:
            colour = style.LINE
        if self.isSelected():
            colour = style.LINE_HIGHLIGHT

        pen = QPen(colour, 2.6 if self.isSelected() else 1.8)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        if self.component.effective_cv() <= 0.0:
            pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.drawLine(self._p1, self._p2)

        mid = (self._p1 + self._p2) / 2
        angle = math.atan2(
            self._p2.y() - self._p1.y(), self._p2.x() - self._p1.x()
        )
        self._draw_arrow(painter, mid, angle, colour)

        painter.save()
        painter.translate(mid)
        # Lay the symbol along its own line, so a valve on a vertical run reads
        # vertically. Flip lines that head leftwards by half a turn to keep
        # asymmetric glyphs (a regulator's dome, a solenoid's coil) upright.
        symbol_angle = angle + math.pi if math.cos(angle) < 0 else angle
        painter.rotate(math.degrees(symbol_angle))
        painter.setPen(QPen(colour, 1.6))
        painter.setBrush(QBrush(style.FILL))
        self._draw_symbol(painter)
        painter.restore()

        t = 0.5 + self.label_shift
        anchor = QPointF(
            self._p1.x() + (self._p2.x() - self._p1.x()) * t,
            self._p1.y() + (self._p2.y() - self._p1.y()) * t,
        )

        # Offset labels perpendicular to the run, so a vertical line gets its
        # labels beside it rather than written straight down the pipe.
        perp = QPointF(-math.sin(angle), math.cos(angle))
        upright = abs(perp.x()) <= 0.5  # line runs mostly horizontally

        label = self.component.name
        if self.component.cv:
            label += f"  Cv {self.component.cv:g}"
        painter.setPen(QPen(style.TEXT_MUTED))
        painter.setFont(_label_font(7))
        rect, flags = _label_box(anchor, perp, -26.0, upright)
        painter.drawText(rect, flags, label)

        if self.result_text:
            painter.setPen(QPen(style.RESULT_TEXT))
            painter.setFont(_label_font(8, bold=True))
            rect, flags = _label_box(anchor, perp, 22.0, upright)
            painter.drawText(rect, flags, self.result_text)

    def _draw_arrow(
        self, painter: QPainter, at: QPointF, angle: float, colour: QColor
    ) -> None:
        offset = 24.0
        tip = QPointF(
            at.x() + offset * math.cos(angle), at.y() + offset * math.sin(angle)
        )
        size = 6.0
        left = QPointF(
            tip.x() - size * math.cos(angle - 0.5),
            tip.y() - size * math.sin(angle - 0.5),
        )
        right = QPointF(
            tip.x() - size * math.cos(angle + 0.5),
            tip.y() - size * math.sin(angle + 0.5),
        )
        painter.setBrush(QBrush(colour))
        painter.setPen(QPen(colour, 1.0))
        painter.drawPolygon(QPolygonF([tip, left, right]))

    def _draw_symbol(self, painter: QPainter) -> None:
        s = style.EDGE_SYMBOL
        half = s / 2
        t = self.component.type

        if t.is_valve or t is ComponentType.REGULATOR:
            # Bowtie: the standard valve glyph.
            bowtie = QPolygonF(
                [
                    QPointF(-half, -half * 0.8),
                    QPointF(0, 0),
                    QPointF(-half, half * 0.8),
                ]
            )
            painter.drawPolygon(bowtie)
            painter.drawPolygon(
                QPolygonF(
                    [
                        QPointF(half, -half * 0.8),
                        QPointF(0, 0),
                        QPointF(half, half * 0.8),
                    ]
                )
            )
            if t is ComponentType.REGULATOR:
                painter.drawChord(
                    QRectF(-half * 0.7, -half * 2.1, s * 0.7, s * 0.7), 0, 180 * 16
                )
                painter.drawLine(QPointF(0, -half * 1.4), QPointF(0, 0))
            elif t is ComponentType.SOLENOID_VALVE:
                painter.drawRect(QRectF(-half * 0.5, -half * 2.0, s * 0.5, s * 0.45))
                painter.drawLine(QPointF(0, -half * 1.55), QPointF(0, 0))
            elif t is ComponentType.CHECK_VALVE:
                painter.drawLine(
                    QPointF(half * 0.7, -half * 0.8), QPointF(half * 0.7, half * 0.8)
                )
        elif t is ComponentType.REDUCER:
            painter.drawPolygon(
                QPolygonF(
                    [
                        QPointF(-half, -half * 0.8),
                        QPointF(half, -half * 0.4),
                        QPointF(half, half * 0.4),
                        QPointF(-half, half * 0.8),
                    ]
                )
            )
        elif t is ComponentType.HOSE:
            path = QPainterPath(QPointF(-half, 0))
            path.cubicTo(
                QPointF(-half / 2, -half),
                QPointF(half / 2, half),
                QPointF(half, 0),
            )
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
        else:
            painter.drawRect(QRectF(-half, -half * 0.55, s, s * 0.55))

    def set_result(self, text: str, choked: bool) -> None:
        self.result_text = text
        self.choked = choked
        self.update()


def assign_ports_for_node(node_item: NodeItem) -> None:
    """Spread the lines meeting at a node across its available anchors.

    Each line wants the leg pointing most nearly towards the node at its other
    end, so a tee fed from the left with two branches leaving to the right and
    below lands on exactly those three legs. Assignment is greedy on best fit
    and keeps one line per leg while legs remain; beyond that (more lines than
    legs, which the model allows) legs are shared.
    """
    edges = list(node_item._edges)
    if not edges or not node_item.ports:
        return

    scored: list[tuple[float, int, EdgeItem, PortItem]] = []
    for index, edge in enumerate(edges):
        outgoing = edge.component.from_node == node_item.node_id
        other = edge.dst if outgoing else edge.src

        # Aim at the far end's actual anchor once it has one, not just the
        # node's centre. Without this, two lines running between the same pair
        # of nodes get identical direction vectors, the tie is broken
        # arbitrarily, and they cross over each other.
        far_port = edge.dst_port if outgoing else edge.src_port
        target = (
            other.mapToScene(far_port.pos())
            if far_port is not None
            else other.scenePos()
        )
        towards = target - node_item.scenePos()
        length = math.hypot(towards.x(), towards.y()) or 1.0
        towards = QPointF(towards.x() / length, towards.y() / length)

        for port in node_item.ports:
            if not port.accepts(outgoing):
                continue
            direction = port.direction()
            # Lower is better: -1 when the leg points straight at the other node.
            cost = -(direction.x() * towards.x() + direction.y() * towards.y())
            scored.append((cost, index, edge, port))

    # `index` breaks ties deterministically, so repeated passes are stable.
    scored.sort(key=lambda item: (item[0], item[1]))

    placed: set[int] = set()
    taken: set[PortItem] = set()
    for _cost, index, edge, port in scored:
        if index in placed or port in taken:
            continue
        edge.set_port(node_item, port)
        placed.add(index)
        taken.add(port)

    # More lines than legs: fall back to the best-fitting leg, sharing it.
    for _cost, index, edge, port in scored:
        if index not in placed:
            edge.set_port(node_item, port)
            placed.add(index)

    for edge in edges:
        edge.refresh_geometry()
