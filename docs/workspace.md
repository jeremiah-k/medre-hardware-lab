# One lab workspace

Run Docker, radio and service campaigns from this checkout. Use the lab CLI with
an explicit `--root` when invoking it from another directory. Keep reusable
orchestration here and call the versioned core/provider harnesses from exact
source checkouts under `sources/`; changes to provider behavior stay in the
provider repository.

| Path                             | Purpose                                                              | Git policy                |
| -------------------------------- | -------------------------------------------------------------------- | ------------------------- |
| `sources/`                       | Exact Docker or qualification source checkouts                       | Ignored except its README |
| `.local/campaigns/`              | Per-campaign uv environments, selected revisions and private records | Ignored                   |
| `profiles/`, `identities/`       | Dedicated native state and private identities                        | Ignored                   |
| `evidence/`, `lab/logs/`         | Raw observations and run output                                      | Ignored                   |
| `lab/state/`, `.env`, `lab.toml` | Generated Docker state and operator configuration                    | Ignored                   |

Create private directories with owner-only permissions. Keep real channel URLs,
mail credentials, connection links, native identities, account databases, Matrix
access tokens and raw peer observations out of commits. Review an explicit list
of staged files before pushing; publish only a separately reviewed sanitized
summary.

The Docker source lock, host uv lock and service qualification targets have
different scopes. Record the exact selection and its installed SDKs for each
campaign. See [service campaigns](service-qualification.md) for publication work.
