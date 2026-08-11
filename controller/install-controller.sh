#!/usr/bin/env bash

set -euo pipefail
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "install-controller.sh must run as root" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly CONFIG_DIR=/etc/gemm-autoresearch
readonly CONFIG_FILE="$CONFIG_DIR/controller.env"
readonly KEY_FILE="$CONFIG_DIR/github-app.pem"
readonly LIBEXEC_DIR=/usr/local/libexec/gemm-autoresearch
readonly SERVICE_FILE=/etc/systemd/system/gemm-autoresearch-controller.service

for required in \
    "$SCRIPT_DIR/gemm_controller.py" \
    "$SCRIPT_DIR/gemmctl.py" \
    "$SCRIPT_DIR/gemm-autoresearch-controller.service"; do
    if [[ ! -f "$required" || -L "$required" ]]; then
        echo "missing or unsafe installation input: $required" >&2
        exit 1
    fi
done

for command in /usr/bin/python3 /usr/bin/openssl /usr/bin/systemctl \
    /usr/sbin/groupadd /usr/sbin/useradd /usr/sbin/usermod; do
    if [[ ! -x "$command" ]]; then
        echo "required command not found: $command" >&2
        exit 1
    fi
done

if ! getent group gemm-submit >/dev/null; then
    /usr/sbin/groupadd --system gemm-submit
fi
if ! id gemm-control >/dev/null 2>&1; then
    /usr/sbin/useradd --system --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin gemm-control
fi
if ! id gemm-agent >/dev/null 2>&1; then
    /usr/sbin/useradd --system --create-home --home-dir /var/lib/gemm-agent \
        --shell /usr/sbin/nologin gemm-agent
fi
/usr/sbin/usermod --append --groups gemm-submit gemm-agent

if [[ ! -f "$CONFIG_FILE" || -L "$CONFIG_FILE" ]]; then
    echo "missing protected configuration: $CONFIG_FILE" >&2
    exit 1
fi
if [[ ! -f "$KEY_FILE" || -L "$KEY_FILE" ]]; then
    echo "missing protected GitHub App key: $KEY_FILE" >&2
    exit 1
fi
for variable in GITHUB_APP_ID GITHUB_INSTALLATION_ID GITHUB_REPOSITORY; do
    if ! /usr/bin/grep -Eq "^${variable}=[^[:space:]]+$" "$CONFIG_FILE"; then
        echo "$CONFIG_FILE is missing $variable" >&2
        exit 1
    fi
done

/usr/bin/chown root:gemm-control "$CONFIG_FILE" "$KEY_FILE"
/usr/bin/chmod 0640 "$CONFIG_FILE" "$KEY_FILE"

/usr/bin/install -d -o root -g root -m 0755 "$LIBEXEC_DIR"
/usr/bin/install -o root -g root -m 0755 \
    "$SCRIPT_DIR/gemm_controller.py" "$LIBEXEC_DIR/gemm_controller.py"
/usr/bin/install -o root -g root -m 0755 \
    "$SCRIPT_DIR/gemmctl.py" /usr/local/bin/gemmctl
/usr/bin/install -o root -g root -m 0644 \
    "$SCRIPT_DIR/gemm-autoresearch-controller.service" "$SERVICE_FILE"

/usr/bin/systemctl daemon-reload
/usr/bin/systemctl enable --now gemm-autoresearch-controller.service
/usr/bin/systemctl --no-pager --full status gemm-autoresearch-controller.service

echo
echo "Controller installed. Test the narrow client with:"
echo "  sudo -u gemm-agent /usr/local/bin/gemmctl status"
