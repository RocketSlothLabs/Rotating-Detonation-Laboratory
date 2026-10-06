"""Property editor for the selected node or component.

The form is rebuilt from the selected object's type. Edits write through to the
model immediately; there is no apply button and no shadow copy of the data.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..model.components import (
    ComponentType,
    FlowComponent,
    Node,
    NodeKind,
    SinkSpec,
    SourceMode,
    ValveSchedule,
    VesselModel,
)
from ..model.library import part_library
from ..units import LITER_TO_M3


def _spin(
    value: float | None,
    minimum: float = 0.0,
    maximum: float = 1e9,
    decimals: int = 3,
    step: float = 1.0,
    suffix: str = "",
) -> QDoubleSpinBox:
    box = QDoubleSpinBox()
    box.setRange(minimum, maximum)
    box.setDecimals(decimals)
    box.setSingleStep(step)
    box.setSuffix(suffix)
    box.setValue(value if value is not None else 0.0)
    box.setKeyboardTracking(False)
    return box


class PropertyPanel(QScrollArea):
    """Edits whichever object the canvas has selected."""

    changed = Signal(str)  # id of the object that changed

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self._target: Node | FlowComponent | None = None
        self._body = QWidget()
        self._layout = QVBoxLayout(self._body)
        self._layout.setContentsMargins(8, 8, 8, 8)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.setWidget(self._body)
        self.show_object(None)

    # --- construction ------------------------------------------------------

    def _clear(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _group(self, title: str) -> QFormLayout:
        box = QGroupBox(title)
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self._layout.addWidget(box)
        return form

    def _notify(self) -> None:
        if self._target is not None:
            self.changed.emit(self._target.id)

    def show_object(self, obj: Node | FlowComponent | None) -> None:
        self._target = obj
        self._clear()

        if obj is None:
            hint = QLabel(
                "Select a component or node on the canvas to edit it.\n\n"
                "Drag node types from the palette onto the canvas, then drag "
                "from a node's right-hand port to another node to connect them."
            )
            hint.setWordWrap(True)
            hint.setStyleSheet("color: #6b7383;")
            self._layout.addWidget(hint)
            return

        if isinstance(obj, Node):
            self._build_node_form(obj)
        else:
            self._build_component_form(obj)

    # --- nodes -------------------------------------------------------------

    def _build_node_form(self, node: Node) -> None:
        form = self._group(f"{node.kind.value.replace('_', ' ').title()}")

        name = QLineEdit(node.name)
        name.textChanged.connect(lambda t: (setattr(node, "name", t), self._notify()))
        form.addRow("Name", name)
        form.addRow("ID", QLabel(f"<code>{node.id}</code>"))

        rotation = QComboBox()
        for angle in (0, 90, 180, 270):
            rotation.addItem(f"{angle}\N{DEGREE SIGN}", float(angle))
        index = rotation.findData(float(int(node.rotation) % 360))
        rotation.setCurrentIndex(index if index >= 0 else 0)
        rotation.currentIndexChanged.connect(
            lambda i: (
                setattr(node, "rotation", rotation.itemData(i)),
                self._notify(),
            )
        )
        form.addRow("Rotation", rotation)

        if node.kind is NodeKind.SOURCE:
            self._build_source_fields(node)
        elif node.kind is NodeKind.ACCUMULATOR:
            self._build_accumulator_fields(node)
        elif node.kind is NodeKind.SINK:
            self._build_sink_fields(node)

    def _build_source_fields(self, node: Node) -> None:
        form = self._group("Supply")

        mode = QComboBox()
        for m in SourceMode:
            mode.addItem(m.value.replace("_", " ").title(), m)
        mode.setCurrentIndex(list(SourceMode).index(node.source_mode))
        form.addRow("Mode", mode)

        pressure = _spin(node.supply_pressure_psig, maximum=1e5, decimals=2, suffix=" psig")
        pressure.valueChanged.connect(
            lambda v: (setattr(node, "supply_pressure_psig", v), self._notify())
        )
        form.addRow("Pressure", pressure)

        temp = _spin(node.supply_temperature_R, minimum=1.0, maximum=5000, decimals=2, suffix=" degR")
        temp.valueChanged.connect(
            lambda v: (setattr(node, "supply_temperature_R", v), self._notify())
        )
        form.addRow("Temperature", temp)

        volume = _spin(
            (node.volume_m3 or 0.0) / LITER_TO_M3, maximum=1e6, decimals=3, suffix=" L"
        )
        volume.valueChanged.connect(
            lambda v: (setattr(node, "volume_m3", v * LITER_TO_M3), self._notify())
        )
        form.addRow("Volume", volume)
        volume.setEnabled(node.source_mode is SourceMode.FINITE_VOLUME)

        def on_mode(index: int) -> None:
            node.source_mode = mode.itemData(index)
            volume.setEnabled(node.source_mode is SourceMode.FINITE_VOLUME)
            self._notify()

        mode.currentIndexChanged.connect(on_mode)

        note = QLabel(
            "Ideal constant holds this pressure indefinitely. Finite volume "
            "blows the tank down during transient runs."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #6b7383; font-size: 10px;")
        form.addRow(note)

    def _build_accumulator_fields(self, node: Node) -> None:
        form = self._group("Vessel")

        volume = _spin(
            (node.volume_m3 or 0.0) / LITER_TO_M3, maximum=1e6, decimals=3, suffix=" L"
        )
        volume.valueChanged.connect(
            lambda v: (setattr(node, "volume_m3", v * LITER_TO_M3), self._notify())
        )
        form.addRow("Volume", volume)

        charge = _spin(node.charge_pressure_psig, maximum=1e5, decimals=2, suffix=" psig")
        charge.valueChanged.connect(
            lambda v: (setattr(node, "charge_pressure_psig", v), self._notify())
        )
        form.addRow("Initial charge", charge)

        temp = _spin(node.charge_temperature_R, minimum=1.0, maximum=5000, decimals=2, suffix=" degR")
        temp.valueChanged.connect(
            lambda v: (setattr(node, "charge_temperature_R", v), self._notify())
        )
        form.addRow("Initial temperature", temp)

        model = QComboBox()
        model.addItem("Auto (isolated -> isentropic)", VesselModel.AUTO)
        model.addItem("Isentropic (isolated)", VesselModel.ISENTROPIC)
        model.addItem("Mass + energy (coupled)", VesselModel.ENERGY)
        model.setCurrentIndex(
            [VesselModel.AUTO, VesselModel.ISENTROPIC, VesselModel.ENERGY].index(
                node.vessel_model
            )
        )
        model.currentIndexChanged.connect(
            lambda i: (
                setattr(node, "vessel_model", model.itemData(i)),
                self._notify(),
            )
        )
        form.addRow("Transient model", model)

        note = QLabel(
            "Isentropic assumes the vessel is cut off from its supply. "
            "Mass + energy tracks inflow and outflow separately and is required "
            "whenever an upstream source keeps feeding it."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #6b7383; font-size: 10px;")
        form.addRow(note)

    def _build_sink_fields(self, node: Node) -> None:
        form = self._group("Engine boundary condition")

        spec = QComboBox()
        spec.addItem("Mass flow only", SinkSpec.MASS_FLOW)
        spec.addItem("Pressure only", SinkSpec.PRESSURE)
        spec.addItem("Both (size the supply)", SinkSpec.BOTH)
        spec.setCurrentIndex(
            [SinkSpec.MASS_FLOW, SinkSpec.PRESSURE, SinkSpec.BOTH].index(node.sink_spec)
        )
        form.addRow("Specify", spec)

        mdot = _spin(
            (node.target_mdot_kgs or 0.0) * 1e3, maximum=1e6, decimals=3, suffix=" g/s"
        )
        mdot.valueChanged.connect(
            lambda v: (setattr(node, "target_mdot_kgs", v * 1e-3), self._notify())
        )
        form.addRow("Target mass flow", mdot)

        pressure = _spin(node.target_pressure_psig, maximum=1e5, decimals=2, suffix=" psig")
        pressure.valueChanged.connect(
            lambda v: (setattr(node, "target_pressure_psig", v), self._notify())
        )
        form.addRow("Target pressure", pressure)

        spec.currentIndexChanged.connect(
            lambda i: (setattr(node, "sink_spec", spec.itemData(i)), self._notify())
        )

        note = QLabel(
            "The engine is always a boundary condition -- no combustion is "
            "modelled. Pinning both quantities lets backward mode solve for the "
            "supply pressure they require."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #6b7383; font-size: 10px;")
        form.addRow(note)

    # --- components --------------------------------------------------------

    def _build_component_form(self, comp: FlowComponent) -> None:
        form = self._group("Component")

        library = QComboBox()
        library.addItem("Custom", "")
        for key, part in sorted(part_library().items(), key=lambda kv: kv[1].name):
            library.addItem(f"{part.name}  (Cv {part.cv:g})", key)
        if comp.library_key:
            index = library.findData(comp.library_key)
            if index >= 0:
                library.setCurrentIndex(index)
        form.addRow("Catalogue part", library)

        name = QLineEdit(comp.name)
        form.addRow("Name", name)

        type_box = QComboBox()
        for t in ComponentType:
            type_box.addItem(t.value.replace("_", " ").title(), t)
        type_box.setCurrentIndex(list(ComponentType).index(comp.type))
        form.addRow("Type", type_box)

        cv = _spin(comp.cv, minimum=0.0, maximum=1e5, decimals=4, step=0.1)
        form.addRow("Cv", cv)

        manufacturer = QLineEdit(comp.manufacturer)
        part_number = QLineEdit(comp.part_number)
        form.addRow("Manufacturer", manufacturer)
        form.addRow("Part number", part_number)

        inlet = QLineEdit(comp.inlet_size)
        outlet = QLineEdit(comp.outlet_size)
        connection = QLineEdit(comp.connection_type)
        form.addRow("Inlet size", inlet)
        form.addRow("Outlet size", outlet)
        form.addRow("Connection", connection)

        rating = _spin(
            comp.max_pressure_psig, maximum=1e5, decimals=1, suffix=" psig"
        )
        form.addRow("Max pressure", rating)

        name.textChanged.connect(lambda t: (setattr(comp, "name", t), self._notify()))
        cv.valueChanged.connect(lambda v: (setattr(comp, "cv", v), self._notify()))
        manufacturer.textChanged.connect(
            lambda t: (setattr(comp, "manufacturer", t), self._notify())
        )
        part_number.textChanged.connect(
            lambda t: (setattr(comp, "part_number", t), self._notify())
        )
        inlet.textChanged.connect(
            lambda t: (setattr(comp, "inlet_size", t), self._notify())
        )
        outlet.textChanged.connect(
            lambda t: (setattr(comp, "outlet_size", t), self._notify())
        )
        connection.textChanged.connect(
            lambda t: (setattr(comp, "connection_type", t), self._notify())
        )
        rating.valueChanged.connect(
            lambda v: (
                setattr(comp, "max_pressure_psig", v if v > 0 else None),
                self._notify(),
            )
        )
        type_box.currentIndexChanged.connect(
            lambda i: (
                setattr(comp, "type", type_box.itemData(i)),
                self.show_object(comp),
                self._notify(),
            )
        )

        def apply_part(index: int) -> None:
            key = library.itemData(index)
            if not key:
                comp.library_key = ""
                self._notify()
                return
            part = part_library()[key]
            comp.library_key = key
            comp.name = part.name
            comp.type = part.type
            comp.cv = part.cv
            comp.manufacturer = part.manufacturer
            comp.part_number = part.part_number
            comp.inlet_size = part.inlet_size
            comp.outlet_size = part.outlet_size
            comp.connection_type = part.connection_type
            comp.max_pressure_psig = part.max_pressure_psig
            if part.outlet_setpoint_psig is not None:
                comp.outlet_setpoint_psig = part.outlet_setpoint_psig
            comp.inlet_min_psig = part.inlet_min_psig
            comp.inlet_max_psig = part.inlet_max_psig
            comp.outlet_min_psig = part.outlet_min_psig
            comp.outlet_max_psig = part.outlet_max_psig
            self.show_object(comp)
            self._notify()

        library.currentIndexChanged.connect(apply_part)

        if comp.type.is_valve or comp.type is ComponentType.REGULATOR:
            self._build_valve_fields(comp)
        if comp.is_regulator:
            self._build_regulator_fields(comp)

    def _build_valve_fields(self, comp: FlowComponent) -> None:
        form = self._group("Actuation")

        is_open = QCheckBox("Open")
        is_open.setChecked(comp.is_open)
        is_open.toggled.connect(
            lambda v: (setattr(comp, "is_open", v), self._notify())
        )
        form.addRow("Steady state", is_open)

        scheduled = QCheckBox("Use a transient schedule")
        scheduled.setChecked(comp.schedule is not None)
        form.addRow("", scheduled)

        schedule = comp.schedule or ValveSchedule()
        open_t = _spin(schedule.open_time or 0.0, maximum=1e5, decimals=3, suffix=" s")
        close_t = _spin(schedule.close_time or 0.0, maximum=1e5, decimals=3, suffix=" s")
        ramp = _spin(schedule.ramp_time, maximum=1e5, decimals=3, suffix=" s")
        starts_open = QCheckBox("Starts open")
        starts_open.setChecked(schedule.initially_open)

        form.addRow("Opens at", open_t)
        form.addRow("Closes at", close_t)
        form.addRow("Ramp time", ramp)
        form.addRow("", starts_open)

        def sync() -> None:
            if not scheduled.isChecked():
                comp.schedule = None
            else:
                comp.schedule = ValveSchedule(
                    open_time=open_t.value() if open_t.value() > 0 else None,
                    close_time=close_t.value() if close_t.value() > 0 else None,
                    ramp_time=ramp.value(),
                    initially_open=starts_open.isChecked(),
                )
            for widget in (open_t, close_t, ramp, starts_open):
                widget.setEnabled(scheduled.isChecked())
            self._notify()

        for widget in (open_t, close_t, ramp):
            widget.valueChanged.connect(lambda _v: sync())
        starts_open.toggled.connect(lambda _v: sync())
        scheduled.toggled.connect(lambda _v: sync())
        for widget in (open_t, close_t, ramp, starts_open):
            widget.setEnabled(scheduled.isChecked())

    def _build_regulator_fields(self, comp: FlowComponent) -> None:
        form = self._group("Regulation")

        regulating = QCheckBox("Holds an outlet setpoint")
        regulating.setChecked(comp.outlet_setpoint_psig is not None)
        form.addRow("", regulating)

        setpoint = _spin(
            comp.outlet_setpoint_psig, maximum=1e5, decimals=2, suffix=" psig"
        )
        form.addRow("Outlet setpoint", setpoint)

        def sync() -> None:
            comp.outlet_setpoint_psig = (
                setpoint.value() if regulating.isChecked() else None
            )
            setpoint.setEnabled(regulating.isChecked())
            self._notify()

        setpoint.valueChanged.connect(lambda _v: sync())
        regulating.toggled.connect(lambda _v: sync())
        setpoint.setEnabled(regulating.isChecked())

        note = QLabel(
            "Clear the setpoint and pick this regulator in Run -> Size regulator "
            "to solve for the outlet pressure the engine requires."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #6b7383; font-size: 10px;")
        form.addRow(note)
