# P&ID Flow Simulator

Interactive design and simulation of pressurized gas feed systems. Lay out a
P&ID on a canvas, assign real manufacturer parts, and solve for pressure,
temperature and mass flow — steady-state in both directions, plus time-domain
accumulator blowdown.

The engine/chamber is always a black-box boundary condition. No combustion or
injector physics is modelled.

## Running

Requires **Python 3.11 or newer**. Clone the repo, then from this directory
(`feed-system-simulator/`):

```powershell
# Windows (PowerShell)
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\pidsim
```

```bash
# macOS / Linux
python3 -m venv .venv
./.venv/bin/python -m pip install -e .
./.venv/bin/pidsim
```

`pip install -e .` pulls in numpy, scipy, matplotlib and PySide6 automatically
(see `pyproject.toml`) and registers the `pidsim` command inside the venv. If
you'd rather not use the console command, `python -m pidsim` works the same
way from an activated venv.

The app opens on a worked example. `File -> Load example` has three, and each
reproduces a validated reference case.

Tests (also needs `pip install pytest` if you skipped it above — it's not a
runtime dependency, only a dev one):

```powershell
.\.venv\Scripts\python.exe -m pip install pytest
.\.venv\Scripts\python.exe -m pytest -q
```

## Layout

```
pidsim/
  units.py          constants and conversions; the single source of truth
  physics/          pure functions -- Cv flow model, gas table, isentropic relations
  model/            components, network graph, part library, project save/load
  solver/           steady nodal solver, transient integrator, vessel dynamics
  gui/              PySide6 canvas, palette, property editor, plots
  data/
    gases.json        gas property table -- edit freely
    components.json   part library -- edit freely
tests/              113 tests; test_validation.py is the gate
```

Dependencies run one way: `gui -> solver -> model -> physics -> units`. No
physics is duplicated in the GUI; if a file under `gui/` ever needs the number
963 or 834, that is a bug.

## Unit conventions

These are load-bearing. They were fixed by reproducing the hand-validated
reference cases, and only this combination does so.

| Quantity | Value |
|---|---|
| SCFH reference state | 14.7 psia, **530 °R** (70 °F) |
| ρ_ref | O₂ 1.3248, CH₄ 0.6641 kg/m³ |
| Flowing temperature | 530 °R default, editable per project |
| ft³ → m³ | 0.0283168 |

The flowing temperature enters the Cv equation and tracks the real gas state.
The SCFH reference temperature only converts standard volume to mass and must
**not** follow it — letting it float silently breaks transient blowdown flows.

## Physics

**Cv flow**, two-regime gas sizing equation, P in psia and T in °R:

- Subsonic (P₂ > 0.5·P₁): `Q[SCFH] = 963·Cv·√((P₁² − P₂²)/(Sg·T))`
- Choked (P₂ ≤ 0.5·P₁): `Q[SCFH] = 834·Cv·P₁·√(1/(Sg·T))`

The branches meet with a 0.002% step at the transition (963·√3/2 = 833.982
against the published 834) — negligible, but the derivative is genuinely
discontinuous there, so nothing downstream assumes smoothness at the choke
point.

**Series combination**: `1/Cv_tot² = Σ 1/Cv_i²`. A standard engineering
approximation, not exact.

**Branch splits**: solved implicitly. Flow divides according to each branch's
own pressure/flow relationship, never evenly by assumption.

**Vessel blowdown**, two formulations kept deliberately separate:

- *Isolated* (cut off from supply): one state, `dP/dt = −ṁ_out·γ·P^(1−1/γ)/K`
- *Coupled* (still fed): two states, `dm/dt` and `dU/dt`, with **inflow
  enthalpy at the source temperature and outflow at the local vessel
  temperature**. That asymmetry is what makes case 5 come out right.

For an ideal gas the two agree when inflow is zero, which the test suite
asserts as a cross-check of the energy formulation against case 4.

## Solver

One unknown pressure per node without a pressure boundary, one mass-balance
equation per unknown node, solved simultaneously in `ln(P)`. Series chains,
splits, merges and parallel paths all fall out of the same residual.

| Node | Pressure unknown? | Contributes an equation? |
|---|---|---|
| Internal junction | yes | yes (net mass flow = 0) |
| Source | no | no — it supplies whatever is drawn |
| Sink, P specified | no | only if ṁ is also specified |
| Sink, ṁ only | yes | yes (net inflow = target ṁ) |
| Backward boundary | yes (the answer) | — |

- **Forward**: supply pressure known → what reaches the engine.
- **Backward**: engine pressure *and* ṁ pinned → the supply pressure that
  requires. `Run -> Size a regulator` targets a named regulator's outlet
  instead of the supply node.
- **Regulators** hold their outlet setpoint, clamped by their own Cv. If they
  cannot pass the demanded flow they are reported as saturated and re-solved as
  a plain restriction.

Transients integrate only vessel state; the rest of the network is solved
quasi-statically at each step (line volume is negligible next to vessel volume).

## Validation

`tests/test_validation.py` is the gate. All five reference cases reproduce well
inside the 1% tolerance, and the nodal solver matches the analytic chain to 14
significant figures.

| Case | Target | Computed |
|---|---|---|
| 1 — O₂ branch, Cv 0.732, 110 g/s | 418.2 psia, choked | 418.41 psia, choked |
| 2 — O₂ main, Cv 2.394, 220 g/s | 473.2 psia | 473.28 psia |
| 3 — CH₄ chain, Cv 1.883, 60 g/s | 197.2 psia | 197.24 psia |
| 4 — isolated accumulator @ 1 s | 331.4 psig, 96.4 g/s | 331.37 psig, 96.29 g/s |
| 5 — coupled accumulator @ 1 s | 431.1 psig, 119 g/s | 431.15 psig, 119.34 g/s |

Two Cv chains fall out of the seeded part library exactly: the 3/4"→3/8"
reducer (2.7) with five 3/8" elements (1.7 each) gives **0.732**, and with
three gives **0.923** — the two values the reference cases use.

## Using the canvas

- Drag a node type (source, tee, accumulator, engine) from the palette onto the
  canvas.
- Pick a catalogue part, then drag from a node's **right-hand port** onto
  another node to place that component between them.
- Click anything to edit it in the Properties dock; edits write straight
  through to the model.
- `Del` removes the selection, wheel zooms, middle-drag pans, `Ctrl+0` fits.
- Run results annotate the diagram directly; choked lines turn red.

## Known v1 limitations

- **Steady-state runs are isothermal** at the project flowing temperature, as
  the validated reference model is. Temperature is a state variable only in
  transient runs.
- **Junction temperatures do not track the gas** passing through them during a
  transient. Vessels carry their own cooling temperature and the Cv equation
  uses the upstream node's temperature, but a plain junction stays at the
  project temperature. The reference cases discharge straight into a fixed
  manifold, so they are unaffected.
- Reverse flow through a component uses a single temperature for both
  directions.

## Out of scope

Multiple engines, recirculating flow loops, real combustion or injector
physics, liquid-phase propellants. Ideal gas throughout.
