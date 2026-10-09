# Rotating Detonation Laboratory

Sizing and analysis codes used by the Rotating Detonation Laboratory, a
student-led research group at ERAU Daytona Beach focused on hardware R&D of
rotating detonation engines.

## Contents

### [`feed-system-simulator/`](feed-system-simulator/)

Interactive design and simulation of pressurized gas feed systems. Draw the
P&ID on a canvas, assign real manufacturer parts, and solve for pressure,
temperature and mass flow.

- **Steady-state, both directions** — given a supply pressure find what reaches
  the engine, or given the engine's demand find the supply pressure it requires
- **Transient** — accumulator blowdown, both isolated and still fed from
  upstream
- **Branching** — splits and merges solved implicitly; flow divides by each
  branch's own pressure/flow relationship, never evenly by assumption

The engine is always a black-box boundary condition (target mass flow and/or
pressure). No combustion or injector physics.

Validated against five hand-checked reference cases for the O₂ and CH₄ feed
trains; all reproduce within 0.3% against a 1% tolerance. See
[`feed-system-simulator/README.md`](feed-system-simulator/README.md) to run it
and [`docs/DEVELOPMENT-NOTES.md`](feed-system-simulator/docs/DEVELOPMENT-NOTES.md)
for the engineering record — unit conventions, design decisions and known
limitations.

```powershell
cd feed-system-simulator
python -m pytest -q      # 129 tests; test_validation.py is the gate
python -m pidsim         # launch the GUI
```

## Contributing

Want to change something, not just run it? See [`CONTRIBUTING.md`](CONTRIBUTING.md)
for the fork/branch/PR workflow.
