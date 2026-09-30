#!/usr/bin/env bash
set -euo pipefail
DEST="workspace/target-pristine"
rm -rf "$DEST"
git clone https://github.com/cosmicpython/code.git "$DEST"
git -C "$DEST" checkout 14c84797ffa77255d53cf1a02fe6aafda2b68aeb
git -C "$DEST" remote remove origin