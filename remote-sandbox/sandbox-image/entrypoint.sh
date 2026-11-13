#!/bin/bash
set -euo pipefail

mkdir -p /root/projects /root/skills /var/lib/kai/sessions /run/kai

# Seed a helpful bashrc once; never overwrite user customizations.
if [[ ! -f /root/.kai_bashrc_seeded ]]; then
  cat >> /root/.bashrc <<'EOF'

# Kai remote sandbox
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$HOME/.local/bin"
alias ll='ls -lah'
EOF
  touch /root/.kai_bashrc_seeded
fi

# Session daemon holds persistent bash sessions for the orchestrator.
exec /usr/local/bin/kai-sessiond
