# Contributing

This repo holds sizing and analysis codes for the Rotating Detonation
Laboratory. Right now that's the [`feed-system-simulator/`](feed-system-simulator/),
with more lab codes likely to land here over time.

## Just want to run it?

No GitHub account or git workflow needed — see
[`feed-system-simulator/README.md`](feed-system-simulator/README.md) for setup
and how to launch it. If you don't want to use git at all, GitHub's
**Code → Download ZIP** button on this repo works too.

## Want to change something?

This project uses the standard GitHub fork workflow.

### One-time setup

1. **Fork this repo** — click Fork at the top of
   `github.com/aprotasov044/Rotating-Detonation-Laboratory`. This creates your
   own copy under your account.
2. **Clone your fork** locally:
   ```bash
   git clone https://github.com/<your-username>/Rotating-Detonation-Laboratory.git
   cd Rotating-Detonation-Laboratory
   ```
3. **Add this repo as `upstream`**, so you can pull in changes other people
   make:
   ```bash
   git remote add upstream https://github.com/aprotasov044/Rotating-Detonation-Laboratory.git
   ```
4. **Set up the simulator's environment** — see
   [`feed-system-simulator/README.md`](feed-system-simulator/README.md). Then
   run the tests before touching anything, to confirm your environment is
   sound:
   ```bash
   cd feed-system-simulator
   python -m pytest -q
   ```
   `tests/test_validation.py` is the gate: it reproduces five hand-validated
   reference cases. If those don't pass, something's wrong with your setup,
   not the code — don't start from a red test suite.

### Making a change

1. **Create a branch** off an up-to-date `main` — don't commit directly to
   `main`, even on your own fork:
   ```bash
   git checkout main
   git fetch upstream
   git merge upstream/main
   git checkout -b my-change-name
   ```
2. Make your change. If you're touching the simulator, read
   [`feed-system-simulator/docs/DEVELOPMENT-NOTES.md`](feed-system-simulator/docs/DEVELOPMENT-NOTES.md)
   first — it records the unit conventions, design decisions, and known traps
   discovered while building it (several of them cost real debugging time once
   already; no reason to rediscover them). In particular: dependencies only
   flow `gui → solver → model → physics → units`, never the other way, and the
   SCFH reference temperature is fixed at 530°R regardless of the flowing gas
   temperature — see that doc before changing either.
3. Add or update tests for what you changed, then run the full suite again:
   ```bash
   python -m pytest -q
   ```
4. Commit with a message that says what changed and why, not just what:
   ```bash
   git add -A
   git commit -m "Short summary of the change"
   ```
5. Push your branch to **your fork**:
   ```bash
   git push origin my-change-name
   ```
6. Open a pull request: on GitHub, go to your fork, and it'll offer to open a
   PR from your branch into `aprotasov044/Rotating-Detonation-Laboratory:main`.
   Describe what the change does and why.

### Keeping your fork in sync

Periodically, and before starting a new branch:

```bash
git checkout main
git fetch upstream
git merge upstream/main
git push origin main
```

## Code style

No formatter or linter is enforced yet. Match the style already in the file
you're editing — this codebase favors explicit docstrings on *why* a
non-obvious choice was made (e.g. `963*Cv*sqrt(...)` is explained with a
comment, not left as a bare constant), not just *what* the code does.
