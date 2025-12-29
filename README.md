# dvx-cli

Installer and manager for dvx, a Claude Code orchestrator that automates implement/review/test/commit cycles.

## Requirements

- Python 3.10+
- Claude Code CLI installed and authenticated

## Installation

```bash
curl -fsSL https://raw.githubusercontent.com/dvx-sh/dvx-cli/main/install-remote.sh | bash
```

Or clone and install locally:

```bash
git clone https://github.com/dvx-sh/dvx-cli.git
cd dvx-cli
./install.sh
```

Add to your shell config (~/.bashrc, ~/.zshrc, etc.):

```bash
export PATH="${HOME}/.dvx/bin:$PATH"
```

## Usage

### dvx commands

```bash
dvx start PLAN-feature.md   # Start orchestrating a plan file
dvx status                  # Show current orchestration status
dvx continue                # Resume after human intervention
dvx decisions               # Show decisions made during orchestration
dvx stop                    # Stop orchestration
dvx reset                   # Clear orchestration state
```

### Upgrade

```bash
cd dvx-cli
git pull
./install.sh
```

## License

Apache 2.0
