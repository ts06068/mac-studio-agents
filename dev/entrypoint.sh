#!/bin/bash
set -euo pipefail
: "${DEV_USER:?DEV_USER is required}"
[[ "$DEV_USER" =~ ^[a-z][a-z0-9_-]{0,30}$ ]] || exit 2
if ! id "$DEV_USER" >/dev/null 2>&1; then
    useradd --create-home --uid 1000 --shell /bin/bash "$DEV_USER"
    usermod -aG sudo "$DEV_USER"
fi
user_home="/home/$DEV_USER"
install -d -o "$DEV_USER" -g "$DEV_USER" -m 700 "$user_home/.ssh" "$user_home/.config/agent"
install -o "$DEV_USER" -g "$DEV_USER" -m 600 /run/secrets/authorized_key "$user_home/.ssh/authorized_keys"
printf '%s:%s\n' "$DEV_USER" "$(cat /run/secrets/login_password)" | chpasswd
chown "$DEV_USER:$DEV_USER" "$user_home" /workspace
chmod 700 "$user_home"
install -d -m 700 /etc/ssh/host-keys
if [[ ! -s /etc/ssh/host-keys/ssh_host_ed25519_key ]]; then
    ssh-keygen -q -t ed25519 -N '' -f /etc/ssh/host-keys/ssh_host_ed25519_key
fi
cat > /etc/ssh/sshd_config <<EOF
Port 22
ListenAddress 0.0.0.0
HostKey /etc/ssh/host-keys/ssh_host_ed25519_key
PermitRootLogin no
PubkeyAuthentication yes
PasswordAuthentication yes
KbdInteractiveAuthentication no
PermitEmptyPasswords no
UsePAM yes
AllowUsers $DEV_USER
AllowTcpForwarding yes
GatewayPorts no
X11Forwarding no
ClientAliveInterval 60
ClientAliveCountMax 3
PrintMotd no
Subsystem sftp internal-sftp
EOF
{
    printf 'export OPENAI_BASE_URL=%q\n' "${OPENAI_BASE_URL:-http://gateway:4000/v1}"
    printf 'export OPENAI_API_KEY=%q\n' "$(cat /run/secrets/llm_api_key)"
    printf 'export LLM_MODEL=%q\n' "${LLM_MODEL:-gemma-4-31b}"
    if [[ -n "${DOCKER_HOST:-}" ]]; then
        printf 'export DOCKER_HOST=%q\n' "$DOCKER_HOST"
        printf 'export DOCKER_TLS_VERIFY=1\n'
        printf 'export DOCKER_CERT_PATH=%q\n' "$user_home/.docker/certs"
    fi
} > "$user_home/.config/agent/env.sh"
chown "$DEV_USER:$DEV_USER" "$user_home/.config/agent/env.sh"
chmod 600 "$user_home/.config/agent/env.sh"
for profile in .profile .bashrc; do
    touch "$user_home/$profile"
    if ! grep -qF '.config/agent/env.sh' "$user_home/$profile"; then
        printf '\n[ ! -f "$HOME/.config/agent/env.sh" ] || . "$HOME/.config/agent/env.sh"\n' >> "$user_home/$profile"
    fi
    chown "$DEV_USER:$DEV_USER" "$user_home/$profile"
done
if [[ -n "${DOCKER_HOST:-}" ]]; then
    for attempt in {1..60}; do
        [[ -s /certs/client/key.pem ]] && break
        sleep 1
    done
    install -d -o "$DEV_USER" -g "$DEV_USER" -m 700 "$user_home/.docker/certs"
    for cert in ca.pem cert.pem key.pem; do
        install -o "$DEV_USER" -g "$DEV_USER" -m 600 "/certs/client/$cert" "$user_home/.docker/certs/$cert"
    done
fi
/usr/sbin/sshd -t
exec /usr/sbin/sshd -D -e

