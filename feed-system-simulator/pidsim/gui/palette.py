"""The component palette: node types to drag onto the canvas, and the part
library to choose from before drawing a connection."""

from __future__ import annotations

from PySide6.QtCore import QMimeData, QSize, Qt, Signal
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import (
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..model.components import NodeKind
from ..model.library import LibraryPart, part_library
from .canvas import MIME_NODE, MIME_PART

NODE_PALETTE = [
    (NodeKind.SOURCE, "Source / Tank", "Supply bottle or regulator outlet"),
    (NodeKind.TEE, "Tee / Branch", "A junction where flow splits or merges"),
    (NodeKind.JUNCTION, "Junction", "A plain connection point"),
    (NodeKind.ACCUMULATOR, "Accumulator", "A vessel with volume; dynamic in transient runs"),
    (NodeKind.SINK, "Engine / Chamber", "The black-box boundary condition"),
]


class _DragList(QListWidget):
    def __init__(self, mime_type: str, parent=None):
        super().__init__(parent)
        self.mime_type = mime_type
        self.setDragEnabled(True)
        self.setAlternatingRowColors(True)
        self.setIconSize(QSize(18, 18))

    def startDrag(self, supported_actions) -> None:
        item = self.currentItem()
        if item is None:
            return
        payload = item.data(Qt.ItemDataRole.UserRole)
        mime = QMimeData()
        mime.setData(self.mime_type, str(payload).encode())
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)


class PalettePanel(QWidget):
    """Node types on top, catalogue parts below."""

    part_selected = Signal(object)  # LibraryPart | None

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        layout.addWidget(QLabel("<b>Drag onto the canvas</b>"))
        self.node_list = _DragList(MIME_NODE)
        for kind, label, tip in NODE_PALETTE:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, kind.value)
            item.setToolTip(tip)
            self.node_list.addItem(item)
        self.node_list.setMaximumHeight(140)
        layout.addWidget(self.node_list)

        layout.addWidget(QLabel("<b>Component for new connections</b>"))
        hint = QLabel(
            "Pick a part, then drag from a node's right-hand port to another "
            "node to place it."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #6b7383; font-size: 10px;")
        layout.addWidget(hint)

        self.part_list = _DragList(MIME_PART)
        generic = QListWidgetItem("Plain line (Cv 10.8)")
        generic.setData(Qt.ItemDataRole.UserRole, "")
        generic.setToolTip("A generic pipe run; edit its Cv afterwards")
        self.part_list.addItem(generic)
        for key, part in sorted(part_library().items(), key=lambda kv: kv[1].name):
            item = QListWidgetItem(f"{part.name}   (Cv {part.cv:g})")
            item.setData(Qt.ItemDataRole.UserRole, key)
            tip = [f"Type: {part.type.value}", f"Cv: {part.cv:g}"]
            if part.manufacturer:
                tip.append(f"{part.manufacturer} {part.part_number}".strip())
            if part.inlet_size:
                tip.append(f"In: {part.inlet_size}  Out: {part.outlet_size}")
            item.setToolTip("\n".join(tip))
            self.part_list.addItem(item)
        self.part_list.currentItemChanged.connect(self._on_part_changed)
        self.part_list.setCurrentRow(0)
        layout.addWidget(self.part_list, 1)

    def _on_part_changed(self, current: QListWidgetItem, _previous) -> None:
        if current is None:
            self.part_selected.emit(None)
            return
        key = current.data(Qt.ItemDataRole.UserRole)
        self.part_selected.emit(part_library().get(key) if key else None)

    def current_part(self) -> LibraryPart | None:
        item = self.part_list.currentItem()
        if item is None:
            return None
        key = item.data(Qt.ItemDataRole.UserRole)
        return part_library().get(key) if key else None
