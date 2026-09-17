# Release runbook (public edition)

This is the operator runbook shipped with the open-source tree. It describes how
to install, verify and operate the services, and how the release of this tree is
produced. It contains no deployment-specific values: every value that differs per
installation is read from the environment.

## 1. Install

```bash
git clone <this repository> antigona && cd antigona
bash install.sh --dev        # creates .venv/ inside the clone and verifies the CLI
```

`install.sh` fails closed: it refuses to claim success unless `import antigona`,
`antigona --version` and `antigona --help` all succeed. It never uses sudo, never
writes outside the clone and never creates or asks for secrets.

## 2. Configuration

Nothing is required for the base CLI. The full stack (gateway, worker, verifier,
delivery) needs storage and provider settings, supplied as environment variables
or an `.env` file at the repository root (git-ignored):

| variable | purpose |
|---|---|
| `ANTIGONA_API_BASE_URL`, `ANTIGONA_API_KEY` | OpenAI-compatible model provider |
| `ANTIGONA_DATABASE_URL` | Postgres/SQLite URL for durable state |
| `ANTIGONA_GATEWAY_URL`, `ANTIGONA_GATEWAY_TOKEN` | gateway address and owner token |
| `ANTIGONA_DELIVERY_TELEGRAM_BOT_TOKEN`, `ANTIGONA_DELIVERY_TELEGRAM_CHAT_ID` | delivery channel |
| `ANTIGONA_OWNER_ID` | owner identity for the approval gate |

The complete list of recognised variables lives in `contracts/config.example.yaml`
and `src/antigona/config.py`. Secrets are never committed and never printed by the
tooling; `.env*`, `*.pem`, `*.key` and runtime databases are git-ignored.

## 3. Services

```bash
deploy/systemd/install_units.sh --help     # unit installation and inspection
```

The gateway API exposes its contract in `docs/gateway_contract.md` and
`docs/gateway_openapi.json`. State lives in the configured database; runtime files
(databases, logs, locks, caches) belong to the deployment, not to the tree.

## 4. Quality gates

Every change must keep all four gates green; a red gate is a defect, not a warning
to be silenced:

```bash
ruff check src tests
python scripts/arch_guard.py     # architecture guard (boundaries + single chokepoint)
mypy src --strict
pytest -q
```

Tests that need docker, network access, credentials or the deployment datasets
skip with an explicit message instead of failing: a green run must be honest even
without those resources.

## 5. Release mechanics

The published tree is produced from a single frozen commit:

1. a sanitization build exports the commit (`git archive`, read-only), keeps only
   whitelisted paths and drops internal audit/ops artifacts;
2. Apache-2.0 `LICENSE` + `NOTICE` are written, path/identifier redactions applied;
3. a hard verification gate fails the build on any surviving owner identifier,
   private IP, absolute home path, secret shape or excluded path;
4. the built tree is scanned with two independent secret scanners plus a regex
   category scan; every finding is triaged in writing;
5. the tree is then published as an orphan branch: one commit, no upstream
   history, no `.git` from the development repository.

Verification after publication is a readback, never the push output: clone the
published branch into a clean directory, re-run the scans there, and compare the
tree hash with the locally built one.

## 6. Rollback

```bash
git push <remote> --delete <release-branch>     # or delete the preview repository
```

The development repository is never modified by the release pipeline: it is read
with `git archive` / `git rev-parse` only.

## 7. Verification environment (sanitized-tree contract)

The published tests assert paths that production derives from the project's
single home resolver, `antigona.core.paths.home_dir()`. A sanitized release
cannot ship the development host's home, so the build rewrites every
host-home literal inside `tests/**` to one pinned public home (/opt/antigona-home) and the
suite must be executed with that same pin:

```bash
ANTIGONA_HOME_DIR=/opt/antigona-home pytest -q
```

Without the pin, the derived-path assertions compare a pinned constant
against the host home and the `tests/unit/test_provenance_guard.py`,
`tests/unit/test_validator_*.py` and `tests/unit/test_turn_worker.py`
derivation tests fail. This is the release pipeline's
sanitizer-vs-product consistency contract (B25); no test is edited and no
gate is relaxed to reach it.

## 8. Reproducing the artifact test run (pinned CI environment, B26)

Two product resolvers decide the paths the sanitized tests assert:
`antigona.core.paths.home_dir()` (the user home) and
`antigona.core.paths.project_root()` (the tree). The sanitizer rewrote
the development host-home literals in `tests/**` onto the single public
home `/opt/antigona-home`, so the suite must run with BOTH pins set:

```bash
export ANTIGONA_HOME_DIR=/opt/antigona-home
# /opt is root-owned on most machines: create the pinned home once with
# elevated rights, then hand it to the running user (the shipped CI does this).
sudo mkdir -p /opt/antigona-home && sudo chown -R "$(id -u):$(id -g)" /opt/antigona-home
cp -a <this tree> /opt/antigona-home/antigona-public
cd /opt/antigona-home/antigona-public
pytest tests -q          # green apart from one documented, owner-gated test
```

Before the run, provision the home layout the sanitized tests expect to
EXIST (empty directories and placeholder credential files only -- nothing
is read from any host home and no secret is involved). The shipped CI
workflow applies it to both the runner home and the pinned home:

```bash
for h in "$HOME" "$ANTIGONA_HOME_DIR"; do
  mkdir -p "$h"/.antigona "$h"/.config "$h"/.hermes/master_prompt "$h"/.gemini \
           "$h"/.local/bin "$h"/.local/share/claude \
           "$h"/.codex "$h"/.claude "$h"/.grok
  echo '{}' > "$h"/.codex/auth.json
done
```

The shipped `.github/workflows/ci.yml` performs exactly these steps, so a
public CI run of this tree reproduces the pinned verification environment
without a hidden step. No test is edited and no gate is relaxed to reach
it: the two pins are the product's own documented environment overrides
(release pipeline v14.1, rule B26).
