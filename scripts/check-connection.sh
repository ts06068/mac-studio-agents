#!/bin/bash
# Run on a developer's computer after enrolling Cloudflare One Client.
set -euo pipefail
api_ip="${1:?Usage: check-connection.sh API_IP SSH_IP [SSH_PORT]}"
ssh_ip="${2:?Usage: check-connection.sh API_IP SSH_IP [SSH_PORT]}"
ssh_port="${3:-22}"
nc -vz -w 5 "$ssh_ip" "$ssh_port"
curl --fail --silent --show-error --max-time 15 "http://$api_ip:4000/readyz"
printf '\n'

