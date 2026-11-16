# Repository guidelines

Read the development and installation sections of [README.md](README.md) before
changing environments or lab setup. Use this checkout as the lab workspace; see
[workspace layout](docs/workspace.md) for local state and source selection.
For Chatmail or SimpleX campaigns, read
[service qualification](docs/service-qualification.md) and invoke the selected
provider harness from exact sources beneath this checkout. Host uv dependencies
and Docker source/bake provenance have separate authorities; qualify and report the actual selection.

```bash
uv sync --locked --extra dev
uv run --no-sync pytest -q
.trunk/trunk check --fix --all
.trunk/trunk check --all --no-fix --ci
```

Use the committed executable launcher. Python policy lives in `pyproject.toml`;
tool versions live in `.trunk/trunk.yaml`. Run `.trunk/trunk upgrade` when
refreshing tools, then fix and check before committing. Keep dependency metadata
and `uv.lock` in the same commit. Select every needed extra on each uv sync.

Shell-sourced configuration is Bash; preserve its parameter expansion semantics.
Run offline tests independently; Docker, native-service and RF qualification are
explicit campaigns. A software or local handoff result is not RF delivery proof.
Preserve source guards and configured identities during cleanup. Keep credentials,
operator profiles, evidence, `.zcode/`, state and logs outside Git.

Use focused commits and describe behavior and validation in the PR. Routine
changes do not require numbered release fragments.
