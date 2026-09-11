#!/bin/bash
# Rebuild py3.12 venvs against py3.14 after the 24.04 -> 26.04 upgrade.
# Package list is derived from the OLD venv's dist-info before it is replaced,
# because the pyproject.toml files do not declare runtime deps.
LOG=/home/a1/venv-rebuild.log
: > "$LOG"
echo "START $(date)" >> "$LOG"

find /home/a1 -maxdepth 3 -name pyvenv.cfg 2>/dev/null | sort | while read -r f; do
  V=$(dirname "$f"); D=$(dirname "$V")
  [ "$(basename "$V")" = ".venv" ] || { echo "SKIP(non-.venv) $V" >> "$LOG"; continue; }
  [ -d "$D/.venv-py312-old" ] && { echo "SKIP(already done) $D" >> "$LOG"; continue; }
  cd "$D" || continue
  PROJ=$(basename "$D" | tr '-' '_')
  SP=$(ls -d "$V"/lib/python3.*/site-packages 2>/dev/null | head -1)
  PKGS=$(ls -d "$SP"/*.dist-info 2>/dev/null | xargs -n1 basename 2>/dev/null \
         | sed 's/-[0-9][^-]*\.dist-info$//' \
         | grep -viE "^(pip|setuptools|wheel|${PROJ})$" | tr '\n' ' ')
  rm -rf "$D/.venv-new"
  python3 -m venv .venv-new >>"$LOG" 2>&1 || { echo "FAIL venv-create $D" >> "$LOG"; continue; }
  .venv-new/bin/pip -q install --upgrade pip >>"$LOG" 2>&1
  if [ -n "$PKGS" ]; then
    .venv-new/bin/pip -q install $PKGS >>"$LOG" 2>&1 || echo "WARN pkgs $D [$PKGS]" >> "$LOG"
  fi
  if [ -f pyproject.toml ]; then
    .venv-new/bin/pip -q install -e ".[dev]" >>"$LOG" 2>&1 \
      || .venv-new/bin/pip -q install -e . >>"$LOG" 2>&1 \
      || echo "WARN editable $D" >> "$LOG"
  fi
  if .venv-new/bin/python -c "import sys" >/dev/null 2>&1; then
    mv "$V" "$D/.venv-py312-old" && mv "$D/.venv-new" "$V" && echo "OK $D [$PKGS]" >> "$LOG"
  else
    echo "FAIL verify $D" >> "$LOG"
  fi
done
echo "DONE $(date)" >> "$LOG"
