#!/usr/bin/env bash
# Convenience wrapper: ./uninstall.sh == ./install.sh --uninstall
set -euo pipefail
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/install.sh" --uninstall
