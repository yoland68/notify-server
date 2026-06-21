#!/usr/bin/env bash
# Install the `notify` CLI into ~/bin (symlinked to the repo, so edits/pulls
# take effect immediately). Re-run any time; it's idempotent.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$REPO_DIR/notify_cli.py"
BIN_DIR="${BIN_DIR:-$HOME/bin}"
DEST="$BIN_DIR/notify"

mkdir -p "$BIN_DIR"
chmod +x "$SRC"
ln -sf "$SRC" "$DEST"
echo "installed: $DEST -> $SRC"

case ":$PATH:" in
  *":$BIN_DIR:"*)
    echo "ready: run 'notify --help'"
    ;;
  *)
    echo
    echo "note: $BIN_DIR is not on your PATH. Add this to your ~/.zshrc:"
    echo "  export PATH=\"\$HOME/bin:\$PATH\""
    echo "then restart your shell (or: source ~/.zshrc)."
    ;;
esac
