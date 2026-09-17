#!/usr/bin/env bash
set -euo pipefail

# install_units.sh - Root-agnostic systemd unit installer for Antigona
#
# SCOPE (B28): this script performs FILE OPERATIONS ONLY.  It never invokes
# systemctl, never runs daemon-reload, never restarts a service, opens no network
# connection and never calls sudo.  It writes exactly two kinds of paths:
#   * target files under --dest (<DEST_DIR>/<unit>.service and, with --dropins,
#     <DEST_DIR>/<unit>.service.d/10-antigona-startup-gate.conf);
#   * backups of overwritten targets under --backup-dir.
# Activating the installed files (daemon-reload / restart) is an OWNER-GATED action
# and is deliberately NOT performed here.
#
# FAIL-CLOSED OVERWRITE POLICY (B28b):
#   * an existing target whose bytes differ from the rendered content is a HARD
#     error (rc=1) unless --force is given.  The check is a PREFLIGHT: it runs over
#     every target before ANY file is written, so a refusal leaves the destination
#     tree completely untouched;
#   * --force makes a byte-identical copy of every differing target under
#     --backup-dir BEFORE that target is rewritten, and prints the backup path;
#   * an existing target with IDENTICAL content is rewritten silently (idempotent),
#     needs no --force and takes no backup.
#
# BASE-UNIT SELECTION (B30): a base ``deploy/systemd/*.service`` template is
#   eligible for installation ONLY when the destination file is absent or already
#   byte-identical to the rendered template.  In drop-in mode (--dropins /
#   --dropins-only) a DIFFERING (e.g. hardened, externally managed) base unit is
#   SKIPPED, not a hard error: it is left byte-identical and the drop-in still
#   lands, because a drop-in is MERGED into an existing unit and must never force
#   the unit's replacement.  Outside drop-in mode the legacy fail-closed refusal
#   is unchanged: a differing base unit aborts the whole run (rc=1, nothing
#   written) unless --force is given.
#
# Usage:
#   deploy/systemd/install_units.sh <ANTIGONA_ROOT> [--dest DIR] [--dry-run]
#       [--check] [--force] [--backup-dir DIR] [--dropins] [--dropins-only]
#
# Modes:
#   --dry-run      render + print every target (path and content); write nothing
#   --check        drift report only; writes nothing; rc=1 iff any target DRIFTED
#   --dropins      install the startup-gate drop-in
#                  (deploy/systemd/dropins/10-antigona-startup-gate.conf) for every
#                  unit, alongside the base units that are eligible (absent or
#                  byte-identical).  A differing/hardened base unit in DEST is
#                  skipped byte-for-byte -- NOT overwritten and NOT a fatal error.
#                  The drop-in is MERGED into the existing unit WITHOUT replacing
#                  it, which is how the fail-closed startup gate reaches an
#                  already-hardened live /etc/systemd/system unit.
#   --check        (B29) additionally reports TEMPLATE-NOT-HARDENED (rc=1) for any
#                  shipped *.service template that lacks the sandbox / privilege
#                  drop, so a reverted template cannot masquerade as a valid source
#                  of truth for the live hardened units.
#   --dropins-only (alias: --no-base-units)
#                  install ONLY the drop-ins; no base ``*.service`` file is ever
#                  written or checked for installation.  Use this to layer the
#                  startup gate onto an externally managed (hardened) unit tree
#                  while guaranteeing that the base units are not touched at all.
#   --force        allow overwriting a differing target (backup-first); outside
#                  drop-in mode this is what turns the fail-closed refusal into a
#                  backup-then-write install
#   --backup-dir   where pre-existing overwritten targets are copied
#                  (default: <DEST_DIR>/.antigona-unit-backups/<UTC timestamp>/)
#
# PLACEHOLDERS (B34): every install-tree or user-home literal in a shipped
# template is a token that render_file() substitutes at install time:
#   @ANTIGONA_ROOT@       -> the ANTIGONA_ROOT positional argument (install tree)
#   @ANTIGONA_ENV_FILE@   -> <home>/antigona.env
#   @ANTIGONA_UV_PYTHON@  -> <home>/.local/share/uv/python
# where <home> is the installing user's home: $ANTIGONA_HOME_DIR when set (the
# project-wide home override honoured by antigona.core.paths.home_dir()), else
# $HOME.  A rendered target that still contains ANY @ANTIGONA_*@ token is a hard
# error (fail-closed, nothing written), so a template can never ship an
# unrendered token.

DEST_DIR="/etc/systemd/system"
BACKUP_DIR=""
DRY_RUN=false
CHECK_MODE=false
FORCE=false
INCLUDE_DROPINS=false
DROPINS_ONLY=false
ANTIGONA_ROOT=""

usage() {
    echo "Usage: $0 <ANTIGONA_ROOT> [--dest <DEST_DIR>] [--dry-run] [--check] [--force] [--backup-dir <DIR>] [--dropins] [--dropins-only|--no-base-units]" >&2
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dest)
            if [[ $# -lt 2 ]]; then
                echo "Error: --dest requires a destination directory argument." >&2
                usage
            fi
            DEST_DIR="$2"
            shift 2
            ;;
        --backup-dir)
            if [[ $# -lt 2 ]]; then
                echo "Error: --backup-dir requires a directory argument." >&2
                usage
            fi
            BACKUP_DIR="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --check)
            CHECK_MODE=true
            shift
            ;;
        --force)
            FORCE=true
            shift
            ;;
        --dropins)
            INCLUDE_DROPINS=true
            shift
            ;;
        --dropins-only|--no-base-units)
            INCLUDE_DROPINS=true
            DROPINS_ONLY=true
            shift
            ;;
        -h|--help)
            usage
            ;;
        *)
            if [[ -z "$ANTIGONA_ROOT" ]]; then
                ANTIGONA_ROOT="$1"
                shift
            else
                echo "Error: Unexpected argument: $1" >&2
                usage
            fi
            ;;
    esac
done

if [[ -z "$ANTIGONA_ROOT" ]]; then
    echo "Error: ANTIGONA_ROOT positional argument is required." >&2
    usage
fi

if [[ ! -d "$ANTIGONA_ROOT" ]]; then
    echo "Error: ANTIGONA_ROOT is not an existing directory: $ANTIGONA_ROOT" >&2
    exit 1
fi

if [[ ! -d "$ANTIGONA_ROOT/src/antigona" ]]; then
    echo "Error: ANTIGONA_ROOT does not contain src/antigona: $ANTIGONA_ROOT" >&2
    exit 1
fi

# Canonicalize ANTIGONA_ROOT to absolute path
ANTIGONA_ROOT="$(cd "$ANTIGONA_ROOT" && pwd -P)"

# ── Home derivation (B34) ─────────────────────────────────────────────────────
# The production env file and the uv interpreter live under the installing user's
# home, never a hardcoded owner path.  $ANTIGONA_HOME_DIR (when set) is the
# project-wide override honoured by antigona.core.paths.home_dir(); otherwise the
# user's $HOME is used.  An undeterminable home is a hard error: an empty prefix
# would silently render a bind of '/' — fail closed instead.
ANTIGONA_HOME="${ANTIGONA_HOME_DIR:-${HOME:-}}"
if [[ -z "$ANTIGONA_HOME" ]]; then
    echo "Error: cannot determine the installing user's home directory." >&2
    echo "Error: set HOME (or ANTIGONA_HOME_DIR) so @ANTIGONA_ENV_FILE@ and" >&2
    echo "Error: @ANTIGONA_UV_PYTHON@ can be rendered." >&2
    exit 1
fi
ANTIGONA_ENV_FILE="$ANTIGONA_HOME/antigona.env"
ANTIGONA_UV_PYTHON="$ANTIGONA_HOME/.local/share/uv/python"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
shopt -s nullglob
UNIT_FILES=("$SCRIPT_DIR"/*.service)
shopt -u nullglob

if [[ ${#UNIT_FILES[@]} -eq 0 ]]; then
    echo "Error: No .service unit templates found in $SCRIPT_DIR" >&2
    exit 1
fi

DROPIN_TEMPLATE="$SCRIPT_DIR/dropins/10-antigona-startup-gate.conf"
if [[ "$INCLUDE_DROPINS" = true && ! -f "$DROPIN_TEMPLATE" ]]; then
    echo "Error: --dropins requested but drop-in template not found: $DROPIN_TEMPLATE" >&2
    exit 1
fi

if [[ -z "$BACKUP_DIR" ]]; then
    BACKUP_DIR="$DEST_DIR/.antigona-unit-backups/$(date -u +%Y%m%dT%H%M%SZ)"
fi

# ── Hardening contract (B29) ──────────────────────────────────────────────────
# Every shipped *.service template MUST carry the privileged drop and the
# service sandbox.  Without this guard a template reverted to the 18-line legacy
# launcher would silently regress a running hardened unit: the installer would
# write it (or --force would overwrite it) and the sandbox would be gone.
# A directive name with '=' matches that directive with any value, so
# 'CapabilityBoundingSet=' (empty value) is matched as present.
HARDENING_DIRECTIVES=(
    "User="
    "Group="
    "NoNewPrivileges=yes"
    "ProtectSystem=strict"
    "ProtectHome="
    "CapabilityBoundingSet="
    "SystemCallArchitectures=native"
    "ReadWritePaths="
    "BindReadOnlyPaths="
)

# missing_hardening <rendered-content> -> echoes each MISSING directive on its own
# line; empty output means the rendered unit satisfies the hardening contract.
# A here-string (not a pipe) is used so the status comes from grep alone: under
# `set -o pipefail` a `printf ... | grep -q` pipeline can report 141 (SIGPIPE)
# when grep exits early, which would spuriously flag a present directive as
# missing.
missing_hardening() {
    local rendered="$1" d
    for d in "${HARDENING_DIRECTIVES[@]}"; do
        if ! grep -q "^[[:space:]]*${d}" <<<"$rendered"; then
            printf '%s\n' "$d"
        fi
    done
}

# render <source-file> -> echoes the content with every @ANTIGONA_*@ placeholder
# substituted.  @ANTIGONA_ROOT@ behaviour is byte-identical to before B34.
render_file() {
    local content
    content="$(cat "$1")"
    content="${content//@ANTIGONA_ROOT@/$ANTIGONA_ROOT}"
    content="${content//@ANTIGONA_ENV_FILE@/$ANTIGONA_ENV_FILE}"
    content="${content//@ANTIGONA_UV_PYTHON@/$ANTIGONA_UV_PYTHON}"
    printf '%s' "$content"
}

# Build the target list: kinds, labels, paths and rendered content (parallel arrays).
TARGET_KINDS=()
TARGET_LABELS=()
TARGET_PATHS=()
TARGET_RENDERED=()

add_target() {
    TARGET_KINDS+=("$1")
    TARGET_LABELS+=("$2")
    TARGET_PATHS+=("$3")
    TARGET_RENDERED+=("$4")
}

for unit in "${UNIT_FILES[@]}"; do
    unit_name="$(basename "$unit")"
    add_target "base" "$unit_name" "$DEST_DIR/$unit_name" "$(render_file "$unit")"
done

if [[ "$INCLUDE_DROPINS" = true ]]; then
    dropin_rendered="$(render_file "$DROPIN_TEMPLATE")"
    for unit in "${UNIT_FILES[@]}"; do
        unit_name="$(basename "$unit")"
        add_target "dropin" "$unit_name.d/10-antigona-startup-gate.conf" \
            "$DEST_DIR/$unit_name.d/10-antigona-startup-gate.conf" \
            "$dropin_rendered"
    done
fi

# Preflight 1 (fail-closed): no target may still carry ANY @ANTIGONA_*@
# placeholder.  A leftover token would ship an unrendered unit (or, worse, a bind
# of the literal token path), so this is a hard error and nothing is written.
for idx in "${!TARGET_PATHS[@]}"; do
    if [[ "${TARGET_RENDERED[$idx]}" =~ @ANTIGONA_[A-Z_]+@ ]]; then
        echo "Error: Unresolved placeholder ${BASH_REMATCH[0]} remains in rendered ${TARGET_LABELS[$idx]}" >&2
        echo "Error: refusing to install a target with an unrendered token." >&2
        exit 1
    fi
done

# ── --check: drift report only, writes nothing ────────────────────────────────
if [[ "$CHECK_MODE" = true ]]; then
    DRIFTED=false
    for idx in "${!TARGET_PATHS[@]}"; do
        target_path="${TARGET_PATHS[$idx]}"
        if [[ -f "$target_path" ]]; then
            if cmp -s <(printf '%s\n' "${TARGET_RENDERED[$idx]}") "$target_path"; then
                echo "${TARGET_LABELS[$idx]}: UP-TO-DATE"
            else
                echo "${TARGET_LABELS[$idx]}: DRIFTED"
                DRIFTED=true
            fi
        else
            echo "${TARGET_LABELS[$idx]}: MISSING"
        fi
        case "${TARGET_LABELS[$idx]}" in
            *.service)
                _missing="$(missing_hardening "${TARGET_RENDERED[$idx]}")"
                if [[ -n "$_missing" ]]; then
                    echo "${TARGET_LABELS[$idx]}: TEMPLATE-NOT-HARDENED (missing: $(printf '%s' "$_missing" | tr '\n' ','))"
                    DRIFTED=true
                fi
                ;;
        esac
    done
    if [[ "$DRIFTED" = true ]]; then
        echo "Drift detected: at least one installed target differs from the shipped template." >&2
        exit 1
    fi
    echo "No drift: every installed target matches the shipped template."
    exit 0
fi

# ── --dry-run: render + print every target, write nothing ─────────────────────
if [[ "$DRY_RUN" = true ]]; then
    for idx in "${!TARGET_PATHS[@]}"; do
        echo "=== [DRY-RUN] ${TARGET_PATHS[$idx]} ==="
        printf '%s\n' "${TARGET_RENDERED[$idx]}"
    done
    exit 0
fi

# ── Preflight 1b (hardening contract, B29): refuse to install a legacy render ──
for idx in "${!TARGET_PATHS[@]}"; do
    case "${TARGET_LABELS[$idx]}" in
        *.service) ;;
        *) continue ;;
    esac
    _missing="$(missing_hardening "${TARGET_RENDERED[$idx]}")"
    if [[ -n "$_missing" ]]; then
        echo "Error: rendered unit ${TARGET_LABELS[$idx]} is missing hardening directive(s):" >&2
        printf 'Error:   %s\n' "$_missing" >&2
        echo "Error: the template set is not hardened; refusing to install it" >&2
        echo "Error: (privileged drop and service sandbox would be lost)." >&2
        exit 1
    fi
done

# ── Target selection (B30): which targets may actually be written ─────────────
# A base unit is eligible only when the destination file is absent or already
# byte-identical to the rendered template.  Outside drop-in mode a differing base
# unit is kept in the write set on purpose, so preflight 2 below still refuses it
# with rc=1.  In drop-in mode (or with --dropins-only) differing base units are
# skipped byte-for-byte: a drop-in must layer onto a hardened unit, never force
# its replacement.
DROPIN_MODE=false
if [[ "$INCLUDE_DROPINS" = true || "$DROPINS_ONLY" = true ]]; then
    DROPIN_MODE=true
fi

INSTALL_INDICES=()
SKIPPED_INDICES=()
for idx in "${!TARGET_PATHS[@]}"; do
    kind="${TARGET_KINDS[$idx]}"
    target_path="${TARGET_PATHS[$idx]}"
    if [[ "$kind" = "dropin" ]]; then
        INSTALL_INDICES+=("$idx")
        continue
    fi
    if [[ "$DROPINS_ONLY" = true ]]; then
        SKIPPED_INDICES+=("$idx")
        continue
    fi
    if [[ -f "$target_path" ]] \
        && ! cmp -s <(printf '%s\n' "${TARGET_RENDERED[$idx]}") "$target_path"; then
        # A differing (hardened/externally managed) base unit.
        if [[ "$DROPIN_MODE" = true && "$FORCE" != true ]]; then
            SKIPPED_INDICES+=("$idx")
            continue
        fi
    fi
    INSTALL_INDICES+=("$idx")
done

if [[ ${#SKIPPED_INDICES[@]} -gt 0 ]]; then
    echo "Skipped ${#SKIPPED_INDICES[@]} base unit(s) that differ from the shipped template" >&2
    echo "and are left byte-identical (hardened/externally managed; drop-ins still apply):" >&2
    for idx in "${SKIPPED_INDICES[@]}"; do
        echo "Skip:   ${TARGET_PATHS[$idx]}" >&2
    done
fi

# ── Preflight 2 (fail-closed, before ANY write): refuse to regress ────────────
DIFFERING_INDICES=()
for idx in "${INSTALL_INDICES[@]}"; do
    target_path="${TARGET_PATHS[$idx]}"
    [[ -f "$target_path" ]] || continue
    if cmp -s <(printf '%s\n' "${TARGET_RENDERED[$idx]}") "$target_path"; then
        continue
    fi
    DIFFERING_INDICES+=("$idx")
done

if [[ ${#DIFFERING_INDICES[@]} -gt 0 && "$FORCE" != true ]]; then
    echo "Error: refusing to overwrite differing target(s); nothing was written:" >&2
    for idx in "${DIFFERING_INDICES[@]}"; do
        echo "Error:   ${TARGET_PATHS[$idx]}" >&2
    done
    echo "Error: the installed file(s) do not match the shipped template; they look" >&2
    echo "Error: like hardened/externally managed units that would be regressed" >&2
    echo "Error: (service sandbox and privilege drop lost)." >&2
    echo "Error: re-run with --force --backup-dir <DIR> after an owner review," >&2
    echo "Error: or use --dropins / --dropins-only to install the gate drop-in without" >&2
    echo "Error: touching the base unit." >&2
    exit 1
fi

# ── Backup-first: every differing target is copied before anything is written ─
WROTE_BACKUP=false
for idx in "${DIFFERING_INDICES[@]}"; do
    target_path="${TARGET_PATHS[$idx]}"
    if [[ "$target_path" == "$DEST_DIR/"* ]]; then
        rel="${target_path#"$DEST_DIR"/}"
    else
        rel="$(basename "$target_path")"
    fi
    backup_path="$BACKUP_DIR/$rel"
    mkdir -p "$(dirname "$backup_path")"
    cp -p "$target_path" "$backup_path"
    WROTE_BACKUP=true
    echo "Backed up $target_path -> $backup_path"
done

# ── Write pass (identical content is rewritten idempotently) ──────────────────
for idx in "${INSTALL_INDICES[@]}"; do
    target_path="${TARGET_PATHS[$idx]}"
    mkdir -p "$(dirname "$target_path")"
    printf '%s\n' "${TARGET_RENDERED[$idx]}" > "$target_path"
    chmod 0644 "$target_path"
    echo "Installed ${TARGET_LABELS[$idx]} -> $target_path"
done

if [[ "$WROTE_BACKUP" = true ]]; then
    echo "Backups written under: $BACKUP_DIR"
fi
