#!/usr/bin/env bash
# Finish the local Zetaris install, after the owner has run the registry sign-in
# (`docker login zetarishackathon.azurecr.io`, pasted from the Zetaris download page).
#
# Run:  bash ~/.private/zetaris/finish-install.sh
#
# It runs Zetaris's own preflight, pulls the images (about 4 GB), starts the stack,
# waits for Zetaris's own verify.sh to pass (the first start under emulation can take
# 30 minutes or more), and connects Stemma's evidence database to Zetaris's network.
# Everything it prints also goes to ~/.private/zetaris/install.log. It never prints
# a password.
set -euo pipefail

Z="$HOME/.private/zetaris"
P="$Z/zetaris-platform"
LOG="$Z/install.log"
# Resource limit: how long to wait for the first start (90 checks, 30 s apart).
TRIES=90

exec > >(tee -a "$LOG") 2>&1
echo "== finish-install $(date -u +%FT%TZ)"
cd "$P"

./preflight.sh

docker compose pull
docker compose up -d

echo "Waiting for the first start; Zetaris's verify.sh is retried every 30 s."
ready=0
for i in $(seq 1 "$TRIES"); do
    if ./verify.sh > "$Z/verify-last.txt" 2>&1; then ready=1; break; fi
    printf '  not ready yet (%d/%d): %s\n' "$i" "$TRIES" \
        "$(grep -m1 FAIL "$Z/verify-last.txt" | sed 's/^ *//')"
    sleep 30
done
cat "$Z/verify-last.txt"
if [ "$ready" -ne 1 ]; then
    echo "verify.sh did not pass in time. The stack is still starting or has failed;"
    echo "run ./verify.sh again in $P, and see: docker compose logs --tail=80 driver api"
    exit 1
fi

docker start stemma-evidence-pg >/dev/null
if docker network inspect zetaris_default \
        --format '{{range .Containers}}{{.Name}} {{end}}' | grep -qw stemma-evidence-pg; then
    echo "stemma-evidence-pg is already on Zetaris's network."
else
    docker network connect zetaris_default stemma-evidence-pg
    echo "stemma-evidence-pg connected to Zetaris's network (zetaris_default)."
fi

cat <<'EOF'

Zetaris is running. One last step for the owner (about 2 minutes):
  1. Open http://localhost:3300 and sign in with the email and password in
     ~/.private/zetaris/admin.txt
  2. Sidebar: Management -> API Tokens -> "+" -> copy the key (shown once).
  3. Add this line to ~/.private/zetaris/stemma.env:  ZETARIS_API_KEY=<the key>
  4. Sidebar: Management -> Organisation. If the ID is not 1, also add
     ZETARIS_ORG_ID=<the ID> to the same file.
Then tell the orchestrator; builder D registers the semantic layer and runs the tests.
EOF
