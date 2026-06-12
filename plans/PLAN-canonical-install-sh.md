# Plan: Make `install.sh` the Canonical dvx Installer

Status: **In Progress**
Created: 2026-06-06

## Implementation Notes

Branch `update-install` has:
- `install.sh` unified with auto-detect (local clone vs piped curl)
- `install-remote.sh` deleted
- README installation section updated
- `AGENTS.md` updated to reflect unified installer shape
- `GOAL.md.example` template added

Remaining before merge:
- Test coverage for install modes
- `--local` / `--remote` explicit modes (plan mentions optional; auto-detect is the primary path)

## Goal

Flip the installer polarity in `dvx-cli` so root `install.sh` is the single
canonical installer for both local checkout installs and remote curl/bootstrap
installs.

The current split has the polarity backwards:

- `install.sh` is local-only.
- `install-remote.sh` is the public remote installer.

For a new user, the clean command should reference `install.sh`, not a special
remote-only script.

## Desired User-Facing Shape

Remote install:

```bash
curl -fsSL https://raw.githubusercontent.com/dvx-sh/dvx-cli/main/install.sh | bash
```

Local checkout install:

```bash
./install.sh
```

Optional explicit modes:

```bash
./install.sh --local
./install.sh --remote
```

## Current State

- Root `install.sh` copies the local `dvx/` payload into `~/.dvx/` and runs
  `~/.dvx/bin/setup`.
- Root `install-remote.sh` downloads the GitHub `main.tar.gz`, extracts it,
  copies the `dvx/` payload into `~/.dvx/`, and runs setup.
- README points the public curl command at `install-remote.sh`.
- `AGENTS.md` documents the two scripts as separate install paths.

## Proposed Design

### 1. Replace `install.sh` with a canonical installer

`install.sh` should support three modes:

- **auto**: default behavior.
  - If the script is running from a local source checkout, install from that
    checkout.
  - If the script is being piped from curl and has no local checkout payload,
    download the GitHub archive and install from that payload.
- **`--local`**: require a local source checkout and fail clearly if one is not
  available.
- **`--remote`**: always download the GitHub archive and install from that
  payload.

Local checkout detection should verify expected payload files, for example:

```text
dvx/bin/setup
dvx/bin/dvx
dvx/src/cli.py
dvx/pyproject.toml
```

### 2. Delete `install-remote.sh`

Remove `install-remote.sh` entirely. There is no compatibility requirement for a
new installer shape unless the maintainers explicitly decide to preserve the old
URL for published users.

If compatibility is needed later, prefer an HTTP redirect or short-lived release
note rather than keeping a second repository script.

### 3. Keep installer safety simple and explicit

The canonical script should:

- install into `~/.dvx/`, unless `DVX_HOME` is introduced deliberately,
- create the destination before resolving paths,
- avoid broad deletes outside the intended `~/.dvx` payload paths,
- clean up temporary download directories with `trap`,
- print whether it is installing from a local checkout or downloaded payload,
- continue to run `~/.dvx/bin/setup` as the environment bootstrap.

### 4. Update docs and agent guidance

Update:

- `README.md` installation section,
- `AGENTS.md` installation script section,
- any tests or plan docs that mention `install-remote.sh`.

New README remote command:

```bash
curl -fsSL https://raw.githubusercontent.com/dvx-sh/dvx-cli/main/install.sh | bash
```

Potential future cleaner domain command if hosted separately:

```bash
curl -fsSL https://dvx.sh/install.sh | bash
```

## Test Plan

Add or update tests/smokes for:

1. `./install.sh --local` from a repo checkout installs to an isolated `HOME`.
2. `./install.sh` from a repo checkout auto-detects local mode.
3. `./install.sh --remote` installs from a local `file://` tarball fixture so the
   test does not depend on GitHub/network availability.
4. Piped/curl-like execution without a local source checkout chooses remote mode.
5. `./install.sh --help` prints mode usage.
6. Unknown flags fail with a non-zero exit and usage text.
7. `install-remote.sh` no longer exists and no docs mention it.

Manual smoke after implementation:

```bash
TMP_HOME=$(mktemp -d)
HOME="$TMP_HOME" ./install.sh --local
HOME="$TMP_HOME" "$TMP_HOME/.dvx/bin/dvx" --help
```

## Acceptance Criteria

- `install.sh` is the only root installer script.
- `install-remote.sh` is removed.
- README public install command points to `install.sh`.
- Local clone install still works with `./install.sh`.
- Explicit `--local` and `--remote` modes work.
- Tests cover local and remote mode without requiring live network access.
- `AGENTS.md` no longer describes `install-remote.sh` as part of the install
  architecture.

## Notes About `install-remote.sh`

`install-remote.sh` was useful as an early split between local development and
remote bootstrap behavior, but keeping both scripts makes the public installer
feel like a special case. Once `install.sh` can choose local or remote mode, the
separate remote script becomes technical debt: duplicate logic, duplicate docs,
and a less clean curl command.
