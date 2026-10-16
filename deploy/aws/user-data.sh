#!/bin/bash
# Paste this into "User data" when launching the instance (Ubuntu 24.04). It installs Docker and
# Python and nothing else. It holds no secrets and opens no ports.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y docker.io docker-compose-v2 python3 python3-venv git
systemctl enable --now docker
usermod -aG docker ubuntu
touch /var/lib/cloud/instance/ready-for-stack
