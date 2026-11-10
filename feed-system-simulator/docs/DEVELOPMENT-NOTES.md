# Development notes — feed system simulator

Engineering record of how this tool was built, what was decided and why, what
was discovered along the way, and where it should not be trusted. Written for
whoever picks this up next.

---

## 1. What this is

A desktop tool for designing pressurized gas feed systems and simulating them:
draw the P&ID on a canvas, assign real manufacturer parts, solve for pressure,
temperature and mass flow. Steady-state in both directions, plus time-domain
accumulator blowdown.

The engine/chamber is **always** a black-box boundary condition — a target mass
flow and/or pressure. No combustion or injector physics. That was a deliberate
scope boundary, not an omission.

Before this existed the analysis lived in one-off hand and Python calculations.
Those were correct but not reusable, not visual, and not something another lab
member could re-run when a valve or line size changed.

---

## 2. Methodology: physics first, GUI last

The build order was deliberate and should be preserved if you extend this:

1. `physics/` — pure functions, no state
2. `model/` — component and network data classes
3. `solver/` — steady and transient, operating only on the model
4. `gui/` — PySide6, calling into solver/model only

Nothing proceeded to the next layer until the five validated reference cases
passed. **This caught real problems early and is the main reason the numbers
can be trusted.**

Dependencies run one way: `gui → solver → model → physics → units`. No physics
is duplicated in the GUI. If a file under `gui/` ever needs the constant 963 or
834, that is a bug.

---

## 3. The single most important finding: unit conventions

The reference numbers are only reproducible under one specific set of
conventions. These were established by implementing the equations and solving
backwards from the known answers, *before* writing any application code.

| Quantity | Value | Consequence if wrong |
|---|---|---|
| SCFH reference state | 14.7 psia, **530 °R** (70 °F) | All mass flows scale wrong |
| ρ_ref | O₂ 1.3248, CH₄ 0.6641 kg/m³ | — |
| Flowing temperature | 530 °R default, per-project | — |
| ft³ → m³ | 0.0283168 | — |
| Reference case 2 total flow | **220 g/s** (two 110 g/s branches) | Reads 473 vs wrong answer |

### The trap

The temperature appears in two places and they are **not** the same number:

- **`t_flow_R`** — the actual local flowing gas temperature. Goes *inside* the
  Cv equation. Tracks the real gas state, and during a transient it drops as an
  accumulator cools.
- **`t_ref_R`** — the SCFH reference temperature. Only converts standard volume
  to mass. **Fixed.**

Letting the SCFH reference float with the flowing temperature is the easy
mistake, and it silently breaks transient blowdown mass flows while leaving
steady-state results looking fine. Both are separate fields in
`ProjectSettings` and the distinction is documented at every call site.

---

## 4. Cv chain decomposition (discovered, not given)

A test written speculatively failed, and working backwards from the failure
recovered how the reference Cv values decompose into catalogue parts:

| Chain | Composition | Cv |
|---|---|---|
| 3/8" branch | reducer (2.7) + **five** 3/8" elements (1.7 each) | **0.732** |
| Accumulator feed | reducer (2.7) + **three** 3/8" elements (1.7 each) | **0.923** |

Both are asserted in `tests/test_network.py`. This matters because it means the
branch Cv is traceable to real part numbers rather than being a magic constant.

Series combination is `1/Cv_tot² = Σ 1/Cv_i²` — a standard engineering
approximation, not exact. It assumes incompressible-style additive resistances
and ignores pressure recovery between elements. It is what the hand-validated
chains used, so the tool reproduces it exactly.

---

## 5. Design decisions

### Nodal solver, not a tree walk

The original spec said "tree-topology solver" but also required branches that
**merge back together**. Those are contradictory — a merge is a cycle in the
undirected graph.

Resolved by using the standard nodal formulation instead:

- one unknown pressure per node without a pressure boundary
- one mass-balance equation per unknown node
- solved simultaneously in `ln(P)` so the solver cannot step to a negative
  absolute pressure (the square roots in the Cv equation would not survive it)

Series chains, splits, merges and parallel paths all fall out of the same
residual. A pure tree is just a special case. Merging is allowed and reported
as INFO; genuine recirculation would need a compressor, which is out of scope.

Bookkeeping that keeps the system square in every mode:

| Node | Pressure unknown? | Contributes an equation? |
|---|---|---|
| Internal junction | yes | yes (net mass flow = 0) |
| Source | no | no — supplies whatever is drawn |
| Sink, P specified | no | only if ṁ is also specified |
| Sink, ṁ only | yes | yes (net inflow = target ṁ) |
| Backward boundary | yes (the answer) | — |

### No networkx

The network is a handful of nodes with domain-specific physics on every edge.
What the solver needs is adjacency plus signed mass-flow residuals — about 100
lines in `model/network.py`. A graph library would have added a dependency
without removing any of the domain work.

### Tees and accumulators are nodes, not edges

A node is a place where a pressure exists; a component is a two-port
restriction between two nodes. So a tee is a **node** (its three legs sit at one
pressure) and an accumulator is a **node with volume**. Only pipes, valves,
hoses, adapters, reducers and regulators are edges.

This maps the reference cases exactly: case 5 is
`source →[Cv 0.923]→ accumulator-node →[Cv 0.732]→ manifold`.

Tee and elbow losses are conventionally folded into the Cv of the adjacent
line, which is what the validated chains do.

### Isolated and coupled vessel models kept separate

Deliberately **not** collapsed into one mode, because they give substantially
different answers when a supply is present (431 psig vs 331 psig at t = 1 s).

- **Isolated** — cut off from supply. Adiabatic and isentropic, one state
  variable: `dP/dt = −ṁ_out·γ·P^(1−1/γ)/K`
- **Coupled** — still fed. Warm gas entering mixes with cold gas inside, so
  entropy is not conserved and two states are needed: `dm/dt` and `dU/dt`

**The asymmetry in `dU/dt` is the whole point**: inflow enthalpy is evaluated at
the *upstream source* temperature, outflow at the *current local vessel*
temperature. Evaluating both at the same temperature is the classic way to get
this wrong.

For an ideal gas the two formulations agree when inflow is zero. That is
asserted as a test, which cross-validates the energy formulation against case 4
— otherwise reachable only through the isentropic path.

### Transients are quasi-steady outside the vessels

Only vessels carry state. At each RHS evaluation the integrator pins the vessel
pressures as boundary conditions and solves the rest of the network
instantaneously. Pipework volume is negligible next to vessel volume, so the
lines equilibrate far faster than the tanks drain. This is what lets an
arbitrary branching network integrate with only one or two state variables per
vessel.

---

## 6. Corrections made during development

Recorded because each cost time and could recur.

**`963·√3/2 = 833.982`, not 834.** An early claim that the regime coefficients
meet *exactly* was wrong. The branches meet with a 0.002% step — negligible
next to any real Cv tolerance. But the **derivative is genuinely discontinuous**
there (flow goes flat once choked), so root finders and ODE integrators must not
assume smoothness at the choke point.

**Forward mode with both engine conditions pinned was adding an equation it
shouldn't.** With the sink specifying pressure *and* mass flow, forward mode was
appending the draw equation that belongs only to backward mode, producing an
ill-posed system. Found by a transient round-trip test, not by inspection.

**PySide6 enum OR-ing silently fails.** `Qt.AlignmentFlag.AlignCenter |
Qt.TextFlag.TextWordWrap` combines two *different* enum types and does not
produce a usable flag — word wrap was doing nothing and node labels overflowed
their boxes. Combine the underlying `.value` ints instead.

**Parallel lines crossed on the canvas.** Two lines joining the same pair of
nodes get identical centre-to-centre direction vectors, so the tie broke
arbitrarily. Fixed with a second assignment pass that refines against the far
end's chosen anchor. There is a test asserting repeated passes reach a fixed
point, since that kind of feedback can oscillate.

---

## 7. A physics question that came up: adiabatic vs isothermal

Asked whether steady-state should use an adiabatic rather than isothermal model.
**For an ideal gas they are the same thing**, and the naive version is actively
harmful.

Flow through a valve is adiabatic with no shaft work and comparable inlet/outlet
velocities, so it is isenthalpic. For an ideal gas enthalpy depends only on
temperature, so the Joule–Thomson coefficient is identically zero and T₂ = T₁.
Isothermal *is* the adiabatic answer here.

The trap is implementing "adiabatic" as isentropic expansion across each
component. On reference case 2 that predicts 294 K → 218 K, and since
Q ∝ 1/√T the mass flow would come out **16% high**. The gas does cool at the
vena contracta but decelerates and recovers downstream. Isentropic expansion is
correct for the *vessel*, where gas does work pushing the remaining contents
out — which is why it is used there and only there.

What the ideal-gas model genuinely omits, for anyone considering going further:

| Effect | Magnitude on case 2 | On mass flow |
|---|---|---|
| Real-gas compressibility (Z ≈ 0.98 at 473 psia, O₂) | ~2% density | ~2% |
| Joule–Thomson cooling (μ ≈ 0.28 K/bar × 21 bar) | ~6 K | ~1% |
| Static cooling from acceleration along the line | ~3 K | ~0.5% |

All are at or below the 1% validation tolerance, and **adding any of them would
move results away from the hand-validated numbers**, because the 963/834
correlation is empirical and already absorbs some of this. Do not add them
casually.

---

## 8. Verification

`tests/test_validation.py` is the gate. 129 tests total.

| Case | Target | Computed |
|---|---|---|
| 1 — O₂ branch, Cv 0.732, 110 g/s | 418.2 psia, choked | 418.41 psia, choked |
| 2 — O₂ main, Cv 2.394, 220 g/s | 473.2 psia | 473.28 psia |
| 3 — CH₄ chain, Cv 1.883, 60 g/s | 197.2 psia | 197.24 psia |
| 4 — isolated accumulator @ 1 s | 331.4 psig, 96.4 g/s | 331.37 psig, 96.29 g/s |
| 5 — coupled accumulator @ 1 s | 431.1 psig, 119 g/s | 431.15 psig, 119.34 g/s |

The nodal solver was separately checked against the analytic chain and matches
to **14 significant figures** with a residual of 3e-16, so solver error is not a
factor — only the physics conventions are.

One subtlety worth knowing: chaining the *exact* branch inlet (418.41 psia) into
stage two gives 473.47 psia, while feeding the *published rounded* 418.2 gives
473.28. Both sit within 0.06% of the reference. Not an error — just rounding
propagation.

---

## 9. Known limitations

Read this before trusting a result.

- **Steady-state is isothermal** at the project flowing temperature, matching
  the validated reference model. Temperature is a state variable only in
  transient runs. See §7 for why this is correct, not a shortcut.
- ~~Junction temperatures do not track the gas~~ -- fixed, see §13. Gas still
  does not cool *gradually along a line's own length* between two nodes;
  lines carry no volume or thermal mass here, so that would need a different
  kind of model, not an extension of this one.
- **Reverse flow uses a single temperature for both directions.**
- A choked feed's flow is set entirely upstream, so forward mode with a mass
  flow target is genuinely over-determined, not a numerical failure. The solver
  detects this and says so in words.

### Out of scope

Multiple engines, recirculating flow loops, real combustion or injector physics,
liquid-phase propellants. Ideal gas throughout.

---

## 10. Environment note

Built against Python 3.12 with numpy, scipy, matplotlib, PySide6 and pytest.

On the development machine `conda create` failed SSL certificate verification
against conda-forge (likely TLS inspection on the campus network), while pip
reached PyPI fine. The workaround was a venv layered on an existing conda
interpreter with `--system-site-packages`. If you hit the same wall:

```powershell
& "<some-python-3.12>\python.exe" -m venv --system-site-packages .venv
.\.venv\Scripts\python.exe -m pip install pytest
```

Rendering under the Qt `offscreen` platform produces tofu boxes for all text,
because that platform has no font provider configured. Under the real Windows
platform — which the app and its PNG export use — text renders correctly. Do
not chase this as a bug.

---

## 11. Making the repo runnable for a stranger (post-upload fixes)

The project was first pushed via GitHub's browser "upload files" drag-and-drop
rather than `git add`/`git commit`, which bypassed `.gitignore` entirely.
Two things slipped through as a result, both fixed in the same pass once
someone other than the original author tried to actually run it:

**Compiled bytecode (30 `.pyc` files) was committed.** `.gitignore` lists
`__pycache__/`, but that only stops *new* untracked files from being added —
it does nothing for a bulk web upload that stages everything on disk
regardless. Removed with `git rm -r --cached`.

**Data file packaging worked by accident, not by design.** `data/` sat at the
project root, a sibling of `pidsim/`, with `package_data = {"pidsim":
["../data/*.json"]}` in `pyproject.toml` escaping the package directory to
reach it. This happened to work on the original dev machine's setuptools
version — confirmed by building a real wheel and inspecting its `RECORD` —
but it is not a documented or guaranteed mechanism, and nothing guarantees a
source distribution (`python -m build --sdist`) would include a `..`-escaped
path at all without an explicit `MANIFEST.in`.

Fixed by moving the data files to **inside** the package
(`pidsim/data/*.json`), which is the standard, supported pattern:
`package_data = {"pidsim": ["data/*.json"]}`, `pidsim/paths.py` resolves
`Path(__file__).resolve().parent / "data"` instead of `.parent.parent`, and a
`MANIFEST.in` covers the sdist case too. Verified by installing into a throwaway
venv both ways — `pip install -e .` (the normal contributor flow) and a real
non-editable `pip install .` — and confirming `get_gas("O2")` resolves
correctly in both.

**Also added**: a `[project.scripts]` entry so `pip install -e .` registers a
plain `pidsim` command, instead of requiring `python -m pidsim`.

**README rewritten.** The original `## Running` / `## Environment` sections
gave the exact path and conda environment of the development machine
(`RDE FEEDLINE\pid-sim`, a specific miniforge `cantera` interpreter) — none of
which exists on anyone else's computer. Replaced with a plain
`venv` + `pip install -e .` flow, given for both PowerShell and bash, using
only the path relative to this file. The conda/SSL detail from §10 above is
historical record of *this* machine's quirks, not a setup requirement, so it
stayed out of the README.

**Verification of the fix, and one more machine-specific wrinkle found while
checking it.** Built a completely clean venv from a bare interpreter (no
`--system-site-packages`), ran `pip install -e .` with nothing pre-installed,
and confirmed all 113 non-GUI tests (physics/model/solver/roundtrip — the
validated engineering core) pass without any special setup. That proves the
packaging fix above is complete and correct.

The GUI tests, however, failed to even *import* PySide6 in that clean venv:
`ImportError: DLL load failed while importing QtCore: The specified procedure
could not be found.` Chased this down rather than writing it off:

- A **freshly `pip install`ed PySide6** (tried both 6.11.2 and 6.12.0, the
  official PyPI wheels) fails this way regardless of which interpreter serves
  as the venv's base — including a bare miniforge interpreter with no
  conda-installed Qt package anywhere near it. So it isn't the conda/Qt DLL
  conflict it first looked like.
- **conda-forge's own PySide6 build** (version 6.11.2, reused via
  `--system-site-packages` rather than pip-installed fresh) imports and runs
  correctly every time — this is what every earlier GUI test and screenshot in
  this project's history actually ran on.

So the PyPI wheel itself is tripping over something specific to this one
machine's accumulated runtime DLLs (plausible culprits: years of overlapping
Visual Studio / Visual C++ redistributable installs and multiple conda
environments, each dropping their own `vcruntime140.dll`/`msvcp140.dll`
somewhere Windows' DLL search order can reach). This is **not** a defect in
the project — the same `pip install PySide6` that fails here is what ordinary
Windows installations run successfully every day — so the README's standard
`pip install -e .` advice was left unchanged; weakening it to work around one
machine's quirk would make it worse for everyone else.

For continued work on *this* machine specifically, the local
`feed-system-simulator/.venv` was (re)built the way that is proven to work:

```powershell
& "<conda-env-with-a-working-PySide6>\python.exe" -m venv --system-site-packages .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m pip install pytest
```

---

## 12. CI

Added `.github/workflows/tests.yml`: runs the full suite on every PR into
`main` and on direct pushes to it, across Ubuntu (3.11, 3.12) and Windows
(3.12).

The Windows leg is a genuine test of the §11 hypothesis above, on a clean
runner with none of this machine's accumulated conda/Visual-Studio history —
if a bare `pip install PySide6` fails there too, that §11 conclusion was
wrong and needs revisiting. If it passes (expected), it's independent
confirmation the DLL issue really was specific to one machine.

The Linux legs need `tlambert03/setup-qt-libs` to install Qt's runtime shared
libraries (libEGL, libxkbcommon, etc.) before PySide6 will import at all on a
bare `ubuntu-latest` runner. Worth noting precisely because it looks similar
to the Windows DLL issue but isn't the same thing: this one is "the shared
libraries genuinely are not installed," not a version conflict between
competing copies. `pytest.importorskip("PySide6")` in `test_gui.py` does not
help here — it only catches `PySide6` not being *installed*, and the failure
mode in both cases is one import deeper than that, at
`from PySide6.QtCore import QPointF`, where the compiled extension tries to
load its native Qt libraries and collection errors out uncaught.

If you hit the identical `DLL load failed ... QtCore` error on a different
machine, it is very unlikely to be this same cause unless that machine also
has a similarly long, overlapping history of conda/VS installs. Worth trying
first: update the Microsoft Visual C++ Redistributable, and/or
`pip install --force-reinstall PySide6`.

---

## 13. Junction temperature propagation

Closed the gap §9 called "the most valuable next improvement": a junction
downstream of a cooling accumulator used to report the flat project default
temperature, and -- the part that actually cost accuracy, not just display --
the Cv equation for whatever component came *after* that junction was
evaluated at that wrong, flat temperature instead of the gas that had
actually arrived.

### What changed

`transient.py` gained `_propagate_node_temperatures`: a small worklist
algorithm that spreads known temperatures (sources at their own
`supply_temperature_R`, vessels at their own ODE-decoded temperature) through
the current flow solution. A node resolves once every component actually
flowing *into* it traces back to an already-known node, and becomes that
mass-weighted average -- the same calculation the vessel energy balance
already needed for its own inflow enthalpy term (`inflow_temperature_K`),
generalised to every node instead of just vessels. A node with no inflow at
all defaults to the project flowing temperature, matching the old behaviour
exactly in that case.

This is deliberately *not* a continuous temperature field along a line's
length -- lines still carry no volume or thermal mass, consistent with the
quasi-steady assumption the whole transient solver already rests on (see the
module docstring). Temperature changes in discrete steps at each node, never
gradually along a run. Extending to a continuous field would be a materially
bigger change (lines would need their own residence time and thermal state)
for accuracy this project's reference cases give no way to validate against.

### The one deliberate approximation: a one-evaluation lag

Computing a junction's temperature needs the flow solution, but the flow
solution (via the Cv equation) needs node temperatures -- so getting both
exactly self-consistent at a single instant would mean solving pressures
twice per RHS evaluation (once to get a flow solution, then again with
corrected junction temperatures fed back in). That doubles the cost of every
`solve_ivp` evaluation, of which there are many per run.

Instead, each junction's temperature is seeded from the *previous*
evaluation's result (`junction_temp_cache`, the same warm-start pattern
already used for the pressure solve's initial guess) and only recomputed
*after* the flow solve, to be used as next evaluation's input. In physical
time this lag is a solver sub-step, not a user-visible output interval --
`solve_ivp` calls the RHS many times between two points a user would ever
actually see -- and the Cv equation's own sensitivity to temperature is weak
(`Q ~ 1/sqrt(T)`), so the residual error this leaves is small by construction
from two directions at once. Measured directly in
`test_downstream_mass_flow_actually_uses_the_propagated_temperature`: about
0.01% relative, against a project-default error of several percent for the
same case -- two orders of magnitude apart, which is the actual argument for
why a single pass is enough and a nested re-solve isn't worth its cost.

### Verification

All five reference cases discharge a vessel directly into a fixed manifold --
no intermediate junction -- so this change is provably a no-op for every one
of them, confirmed by `test_validation.py` and the case 4/5 transient tests
passing unchanged. The new behaviour needed a topology none of the reference
cases exercise (vessel -> junction -> a second component), added as
`junction_network()` and seven tests in `test_transient.py`:

- a junction with one inflow reports *exactly* that inflow's temperature
  (mathematically exact for a one-term weighted average, not an
  approximation -- asserted to `rtol=1e-9`)
- it genuinely cools over the run rather than sitting flat
- the component *after* the junction produces the mass flow the physics
  functions predict at the junction's real temperature, measurably different
  from what the old flat-default behaviour would have given
- the sink (engine) itself now reports the temperature of gas actually
  arriving -- new information this fix makes available, not just a
  junction-specific detail
- two inflows at genuinely different temperatures (a bypass straight from the
  source, mixed with the accumulator's cooled discharge at the same junction)
  produce the correct mass-weighted average, strictly between the two inputs
- a source with a non-default `supply_temperature_R` is now reported as
  itself rather than silently falling back to the project default (the same
  root cause, just for a source instead of a junction)
- both branches leaving a tee agree with the tee's own propagated temperature

---

## 14. Regulator droop

The old regulator model was binary: a regulator either held its outlet
setpoint exactly, or -- once its own Cv genuinely couldn't keep up -- was
flagged saturated and re-solved as a plain restriction. Both ends of that are
physically correct limits (a lightly-loaded regulator really does hold
setpoint almost exactly; a fully-open one really is just an orifice), but a
real regulator's outlet droops *progressively* as demand rises well before it
is fully open, which the binary switch skipped entirely.

### Design: reuse `cv`, add one field

Rather than a disconnected "droop slope" in made-up units, the new
`outlet_droop_psi` field is defined as *the pressure droop at the
regulator's own rated (fully open) capacity* -- a single number from a
manufacturer's droop/regulation spec. "Rated capacity" itself needs no new
field: it is just the regulator's own `cv`, evaluated via the normal Cv
equation from the current inlet pressure down to `setpoint - outlet_droop_
psi`. That reuses `cv` for the same physical meaning it already had (the
fully-open orifice size), rather than introducing a second, disconnected
notion of "capacity."

Three segments as a function of the regulator's *current* outlet pressure,
continuous by construction, not smooth (the same acceptable kind of
non-smoothness the plain Cv equation already has at its choked/subsonic
transition):

- at or above setpoint: no flow needed, clamp to zero
- within the droop band: linear, `mdot = rated * (setpoint - P2) / droop`
- below the full-droop floor: wide open, reverts to the ordinary Cv equation
  between the real inlet and outlet pressures -- exactly the existing
  saturated-regulator behaviour, now reached as this curve's natural
  endpoint instead of a separate code path

### Why this didn't need new solver architecture

A non-drooping regulator's outlet node is pinned to a *fixed* pressure
(`self.fixed`), with a separate `saturated_regulators` set and an outer
re-solve loop (`_check_regulators`) to flip it to the unregulated path when
needed. A drooping regulator's outlet needs none of that: it is simply left
to fall through to the same unknown-pressure-with-a-mass-balance-equation
path every plain junction already gets, and `component_mdot` evaluates the
droop formula above *as a function of that node's current solver iterate*
instead of a fixed target. The existing nodal solver finds the
self-consistent (P2, mdot) pair the same way it finds any junction's
pressure -- droop and full saturation alike, within one solve, no outer loop.

### A test design mistake worth recording

The first version of the "beyond rated capacity" test used the same 2000
psig bottle as the other regulator tests, with a 400 psig setpoint. It failed
-- not because the implementation was wrong, but because the test's own
numbers put the regulator's "rated" reference point already *past the choke
point* (downstream of half the inlet pressure). Once choked, flow is capped
at its maximum by definition and literally cannot exceed "rated" no matter
how much downstream demand increases -- there was no "beyond rated" regime
left for the test to reach. Fixed by lowering the test's supply pressure so
the whole droop band stays subsonic, where flow can still rise as pressure
drops further. Left as `supply_psig` on the `regulated_network()` test
helper. Worth remembering for anyone adding more droop tests: check where the
choke point sits relative to the setpoint and droop band before assuming a
given demand level will produce the behaviour you expect.

### A GUI bug found while rendering the new field to check it

Rendering the regulator property panel to confirm the new field looked right
turned up a real, pre-existing bug: switching from one selected object to
another left the *previous* object's form visibly overlapping the new one.
`PropertyPanel._clear()` removed old widgets from the layout via `takeAt()`
and scheduled `deleteLater()`, but `deleteLater()`'s actual destruction is
deferred to the next event loop iteration -- and taking a widget out of a
layout does not itself hide it. In a continuously-running real application
this gap is normally a single event loop iteration, likely never visibly
perceptible, but a one-line fix (`widget.hide()` before `deleteLater()`) is
the standard, correct way to avoid relying on that timing at all. Confirmed
with a regression test using `isHidden()`, not `isVisible()` --
`isVisible()` depends on the entire ancestor chain being shown, which it
never is in a headless test, so it cannot distinguish the fix from the bug.

---

## 15. Seeding the part library from the lab's real inventory

The RDL Master Inventory spreadsheet (compiled from purchase orders and the
lab's own part inventory, maintained outside this repo) has a "Propellant
Feed System" section with real McMaster-Carr and Grainger part numbers for
hardware the lab physically owns. Looked these up directly against
McMaster's own published specifications rather than taking the BOM's item
name at face value, the same discipline as every other number in this
project.

### Confirmed and added

Three seeded Cv values are now tied to a specific owned part, verified
against McMaster's spec table for that exact part number:

| Seed entry | Part | McMaster spec |
|---|---|---|
| `ball_valve_075` | 46325K29 | Cv **10**, 800 psi @ 100F |
| `check_valve_075_mm` | 1874N18 | Cv **7.4**, 3000 psi @ 70F, 1 psi cracking |
| `reducer_075m_038f` | 48805K749 | 5300 psi @ 72F (no Cv published for this part) |

The first two are genuine cross-validation, not a circular check: both Cv
values were already in this library, originally derived from the validated
hand-calculated reference cases (see the file-level comment in
`components.json` on how the reducer + hose chain produces 0.732 and 0.923).
An independent source -- the manufacturer's own spec table for the exact
part the lab owns -- gives the identical numbers. That is about as strong a
confirmation as this project can get without an instrumented test stand (see
§3 and the physics-validity discussion in the project history: this closes
the "is the Cv itself right" question for these two specific parts, which no
amount of solver-side validation could ever touch).

### What this lookup could and couldn't do

McMaster's product pages are JS-rendered and intermittently returned empty
content to automated fetching -- not a hard block, since several lookups on
the exact same URL pattern (`mcmaster.com/product/<part>/`) succeeded, just
unreliable. Burning many retries chasing full coverage on a source behaving
this way isn't worth it; three solid, verified data points plus knowing
which lookups are still open is more useful than an inconsistent full sweep.

Real parts identified in the BOM but **not** added to the seed, because no
reviewed source gave a verified Cv or because they don't map to anything the
solver models -- listed here so whoever has datasheet access next doesn't
have to re-derive which parts are worth chasing:

- **4443K413** (3/4" M to 1/4" F reducer) and **5288N15** (1/4"-3/4" bushing
  adapter) -- real reducing fittings the lab owns, but a 3/4->1/4 reduction
  is meaningfully more restrictive than the 3/4->3/8 one already seeded, so
  its Cv cannot be assumed from that one. Worth adding once looked up
  directly.
- **5482K21** (3/4" precision 37-degree flared fitting, i.e. JIC) -- McMaster
  page would not render for this one across several attempts.
- Three regulators with real part numbers (**46Z484** nitrogen, plus a CDL
  LX65 oxygen regulator and an Airgas propane regulator with no catalog part
  number at all) -- none of the three reviewed sources publish a Cv for any
  of them. A regulator entry with no Cv is not just incomplete, it is
  actively unusable for sizing (the model would read it as Cv=0, i.e.
  blocking all flow), so these were left out entirely rather than seeded
  with a placeholder.
- **Pressure Relief Valve, 3/4"** (Grainger 60UL47) -- not just missing data:
  there is no `ComponentType` for a relief valve in this model at all. A
  relief valve is normally *closed* until a cracking pressure, then opens --
  categorically different behaviour from every existing type, none of which
  model a pressure-triggered state change. Mapping it onto `ball_valve` or
  `check_valve` would misrepresent how it actually behaves. Modeling it
  properly is a model-layer addition (a new `ComponentType.RELIEF_VALVE` with
  its own open/closed state driven by inlet pressure), not a library seed --
  noted here as a real gap surfaced by real inventory, not a hypothetical one.
- **Tee Connector SS, 3/4"** (McMaster 4443K645) -- the lab owns 9 of these,
  but a tee is a `NodeKind.TEE` in this model, not a `FlowComponent`; the
  part library only ever instantiates two-port restrictions. There is
  nothing to seed here -- the user places a tee node on the canvas, there is
  no "part" standing in for it the way there is for a valve or fitting.

---

## 16. Solver robustness at scale

Set out to answer one question: does the nodal solver's primary method
(`scipy.optimize.root`, `method='hybr'`) stay reliable as a network grows, or
does it start quietly needing the slower `least_squares` fallback? Built two
synthetic network generators to find out, and ended up finding something
more important than the original question.

### Wide branching off one tee is nearly free

First instinct -- fan a source out through one tee into many parallel
branches, all merging at the engine -- turned out to not stress the solver
at all. Up to 120 parallel branches, the unknown count stayed at exactly
**one** (the tee's own pressure) regardless of branch count, since every leg
shares that single node. More branches just means more *equations* evaluated
at a fixed set of unknowns, which is cheap (wall time stayed under 20 ms even
at 120 branches). This is a genuinely useful finding on its own: an injector
manifold with a very large number of parallel feed branches off one tee
poses essentially no solver difficulty, however wide it gets.

### What actually scales: repeated split-merge stages

Unknown count only grows with repeated split-then-merge-back stages in
series (a "ladder" network: each stage contributes two new junction
pressures). Stress-testing that shape found:

- The primary method (`hybr`) **never once** needed the `least_squares`
  fallback, up to 200 simultaneous unknowns (401 components) -- always
  converging to residuals many orders of magnitude tighter than the 1e-9 bar
  the solver itself uses to decide whether the fallback is needed.
- A 10,000x Cv mismatch between parallel branches within the same stage left
  residuals and evaluation counts essentially unchanged. The `ln(P)`
  parameterization keeps this well-conditioned regardless of how extreme the
  mismatch is.
- Wall-clock time for a single solve, however, grows noticeably faster than
  linearly with unknown count: 30 unknowns ~190 ms, 100 unknowns ~3.8 s, 150
  ~16.3 s, 200 ~28.5 s -- consistent with the O(N) finite-difference
  Jacobian estimate and O(N^3) dense linear algebra `hybr`'s underlying
  MINPACK routine does internally, with no sparsity exploited.

This is a genuine, documented limit, deliberately **not** fixed here: making
it scale better would mean supplying an analytic or sparse Jacobian and
switching solver methods -- a real rewrite of the numerical core, not a small
change, and one the project has no evidence it actually needs. Given fan-out
is nearly free, reaching 100+ *unknowns* requires on the order of 50 genuine
split-merge stages in series, which is a far deeper topology than any real
propellant feed system this lab has built or is likely to build. A
regression test (`test_a_realistically_large_branching_network_solves_fast_
without_fallback`) covers 60 unknowns / 241 components -- deeper than
anything built so far -- confirming it stays fast and fallback-free; the
full sweep to 200 unknowns was exploratory, not committed as a test, because
30 seconds is too slow for the suite to pay on every run for a size the
project doesn't need yet.

### The bigger find: transient runs can explode near equilibrium, independent of network size

Pushing into the transient solver (since `solve_quasi_steady` runs once per
RHS evaluation, so whatever the steady solver costs gets paid repeatedly)
found something that had nothing to do with branching complexity at all. A
**trivially simple** one-component network (accumulator discharging directly
into a fixed backpressure, zero branching) needed:

| Run to | `solve_quasi_steady` calls | time in it | vessel pressure |
|---|---|---|---|
| t=1.0 s | 54 | 1 ms | 410 psig (target 400) |
| t=2.0 s | 20,076 | 1.4 s | 400.00 psig |
| t=4.0 s | 96,294 | 1.9 s | 400.00 psig |

Per-call cost stayed tiny throughout (microseconds) -- almost all the
growth was in `solve_ivp`'s own step count, not the nodal solve. As the
vessel's pressure asymptotically approaches its backpressure, `dP/dt`
shrinks toward (but never exactly reaches) zero, and resolving that
ever-flatter tail under the solver's tight `rtol=1e-8` costs an exploding
number of integration steps to add essentially nothing to the reported
curve past the point where it is already indistinguishable from equilibrium.

Combined with a complex (many-unknown) network, where each of those calls
costs milliseconds instead of microseconds rather than cents, the two
effects multiply: a 2-stage, 3-branch ladder network run far enough to
approach its own backpressure took over 5 minutes and had not finished --
not because of a bug, but because it was correctly, slowly grinding through
tens of thousands of expensive re-solves chasing a curve that had already
visually flattened out.

### The fix: a terminal "effectively at equilibrium" event

`solve_transient` gained `stop_at_equilibrium` (default on) and
`equilibrium_rtol` (default 1e-4), mirroring the existing `stop_at_ambient`
pattern. A single combined event tracks each vessel's **net** mass flow
(`mdot_in - mdot_out`, the same quantity `rhs()` already uses to drive
`dP/dt`/`dm/dt`) against a running peak for that vessel, and terminates the
whole integration once every vessel's net flow has decayed to within
`equilibrium_rtol` of its own peak.

Two things worth being deliberate about:

- **Net, not gross, flow.** A *coupled* vessel being continuously fed can
  settle at a large, steady, nonzero throughput once inflow balances
  outflow -- gross flow staying large must never prevent the stop, only the
  net flow that actually drives the vessel's state should. Checked directly
  with a dedicated test (`test_equilibrium_stop_uses_net_not_gross_flow_
  for_a_coupled_vessel`): the coupled case reaches a stop with substantial
  feed and discharge flow both still present.
- **Skipped automatically when a valve schedule has a future transition
  pending.** A scheduled valve closing and reopening would create a
  temporary flow lull that an unconditional check could mistake for true
  equilibrium, permanently (it is a *terminal* event) cutting off real
  dynamics that were about to resume. Rather than trying to detect "a
  temporary dip" from inside an instantaneous zero-crossing check, the
  simpler and safer rule is: if any component's schedule still has an
  open_time or close_time, don't add the event at all. Confirmed with
  `test_equilibrium_stop_is_skipped_with_a_pending_valve_schedule`.

Result, re-measured on the same trivial network: t=2.0 s dropped from 20,076
calls / 1.4 s to 164 calls / 6 ms; t=4.0 s from 96,294 calls / 1.9 s to 186
calls / 8 ms -- both reaching the identical 400.00 psig answer, just without
grinding through the flattened-out tail to get there.

**Honest tradeoff, not a free lunch.** The event function itself needs its
own `snapshot()` call, separate from `rhs()`'s -- real added cost per step,
roughly doubling it. For the large, already-expensive ladder network
*before* it reaches equilibrium, this made intermediate-length runs slower,
not faster (measured directly: a 0.5 s run went from 8,114 calls / 12.9 s to
13,756 calls / 26.3 s). But that same network, pushed to where it actually
reaches equilibrium (t=0.7 s), terminated at the exact right answer (150.00
psig) in 25 s with the fix, against an unfixed run that was still running,
with no end in sight, after 5+ minutes. The event is a bet that bounding the
worst case is worth roughly doubling the cost of runs that don't hit it --
left on by default because a user accidentally running a transient
simulation well past where anything interesting still happens is a more
likely accident than deliberately picking a large network and a short,
equilibrium-avoiding window. `stop_at_equilibrium=False` is there for anyone
who has measured otherwise for their own case.

### Verification

Full suite passes throughout, including the existing case 4/5 transient
tests completely unchanged -- both end at t=1.0 s, nowhere near their own
equilibrium, so the new event never fires for either and was never expected
to. Five new tests cover the equilibrium-stop mechanism directly (early
termination at the right pressure, the opt-out, the schedule gate, and the
net-vs-gross distinction for a coupled vessel); two more exercise the
steady solver at a realistic "deeper than anything built so far" scale.
