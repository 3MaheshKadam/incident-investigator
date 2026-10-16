#!/bin/bash
# Copy this repository to an EC2 instance, start the stack, and run the deployment checks there.
#
#   deploy/aws/deploy.sh <public-ip-or-dns> <path-to-key.pem>
#
# Needs: ssh and scp locally. Uses the committed files only (git archive), so nothing from your
# .env or working tree leaves your machine.
set -euo pipefail

HOST="${1:?usage: deploy.sh <host> <key.pem>}"
KEY="${2:?usage: deploy.sh <host> <key.pem>}"
SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=accept-new "ubuntu@$HOST")

cd "$(git rev-parse --show-toplevel)"
ARCHIVE="$(mktemp -t stack-XXXXXX.tgz)"
trap 'rm -f "$ARCHIVE"' EXIT
git archive --format=tar.gz HEAD -o "$ARCHIVE"

echo "== waiting for the instance to finish its setup"
for _ in $(seq 1 40); do
  if "${SSH[@]}" test -f /var/lib/cloud/instance/ready-for-stack 2>/dev/null; then break; fi
  sleep 5
done

echo "== copying the code"
scp -i "$KEY" "$ARCHIVE" "ubuntu@$HOST:/tmp/stack.tgz"

echo "== starting the stack and running the checks on the instance"
"${SSH[@]}" 'bash -s' <<'REMOTE'
set -euo pipefail
rm -rf ~/stack && mkdir ~/stack && tar -xzf /tmp/stack.tgz -C ~/stack && cd ~/stack
cp -n .env.example .env
sudo docker compose --profile load up -d --build
python3 -m deploy.verify --wait 120 --detect
REMOTE
echo "== done. See the gateway from your laptop with:"
echo "   ssh -i $KEY -L 8080:localhost:8080 ubuntu@$HOST     then open http://localhost:8080/health"
