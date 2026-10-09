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
- **Junction temperatures do not track the gas.** Vessels carry their own
  cooling temperature and the Cv equation uses the upstream node's temperature,
  but a plain junction stays at the project temperature. The reference cases
  discharge a vessel straight into a fixed manifold so they are exact; a long
  branched line downstream of a blowing-down accumulator would read slightly
  warm. **This is the most valuable next improvement** — propagating stagnation
  temperature through the network is the physically correct meaning of adiabatic
  here, is provably a no-op for all five reference cases, and closes the only
  temperature gap that actually costs accuracy.
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

If you hit the identical `DLL load failed ... QtCore` error on a different
machine, it is very unlikely to be this same cause unless that machine also
has a similarly long, overlapping history of conda/VS installs. Worth trying
first: update the Microsoft Visual C++ Redistributable, and/or
`pip install --force-reinstall PySide6`.
