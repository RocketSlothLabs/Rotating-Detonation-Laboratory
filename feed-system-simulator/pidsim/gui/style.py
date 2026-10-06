"""Shared colours and geometry for the canvas."""

from __future__ import annotations

from PySide6.QtGui import QColor

# Canvas
BACKGROUND = QColor("#f7f8fa")
GRID_MINOR = QColor("#e6e9ef")
GRID_MAJOR = QColor("#d4d9e2")

# Items
OUTLINE = QColor("#2c3442")
FILL = QColor("#ffffff")
FILL_SELECTED = QColor("#dbe7ff")
TEXT = QColor("#2c3442")
TEXT_MUTED = QColor("#6b7383")

LINE = QColor("#3a4454")
LINE_HIGHLIGHT = QColor("#1f6feb")
LINE_CHOKED = QColor("#d1242f")
LINE_SHUT = QColor("#9aa3b2")

PORT_IDLE = QColor("#8b94a5")
PORT_HOVER = QColor("#1f6feb")
PORT_VALID = QColor("#1a7f37")

RESULT_TEXT = QColor("#0a3069")
WARNING_TEXT = QColor("#9a6700")

# Geometry
NODE_W = 96.0
NODE_H = 56.0
PORT_R = 5.0
EDGE_SYMBOL = 26.0
GRID = 20.0
