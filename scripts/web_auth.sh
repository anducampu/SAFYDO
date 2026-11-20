#!/usr/bin/env bash
# SAFYDO — optional HTTP basic authentication for the web UI.
#
# nginx does the checking, so the page, the API and the status WebSocket are all
# behind it (a backend-only check would leave the UI itself open). The board
# itself is exempt (127.0.0.1 / ::1): usb_automount.sh and other local callers
# reach the API through nginx, and the TUI talks to :8000 directly anyway.
#
#   web_auth.sh status                 JSON: enabled, username, default_password
#   web_auth.sh enable [USER]          password on stdin (empty = keep the current
#                                      one, or "safydo" when there is none yet)
#   web_auth.sh disable
#   web_auth.sh apply                  re-create the nginx include from player.yaml
#                                      (install.sh runs this on every install)
#
# Files: /etc/nginx/safydo-auth.conf (the include: present = auth on) and
# /etc/nginx/safydo-auth.htpasswd (user:SHA-512-crypt, root:www-data 0640).
# The password never appears on a command line: openssl reads it from stdin.
#
# Locked out? Over SSH: sudo /opt/rpi-audio-player/scripts/web_auth.sh disable
#
# Basic auth over plain HTTP sends the password base64-encoded, i.e. readable by
# anyone sniffing the LAN. It keeps casual users out; it is not encryption.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_common.sh
source "${SCRIPT_DIR}/_common.sh"

NGINX_DIR="${SAFYDO_NGINX_DIR:-/etc/nginx}"
INCLUDE="${NGINX_DIR}/safydo-auth.conf"
HTPASSWD="${NGINX_DIR}/safydo-auth.htpasswd"
DEFAULT_USER="safydo"
DEFAULT_PASS="safydo"
REALM="SAFYDO"

valid_user() { [[ "$1" =~ ^[A-Za-z0-9._-]{1,32}$ ]]; }

current_user() {
    [[ -f "$HTPASSWD" ]] && head -1 "$HTPASSWD" | cut -d: -f1 || true
}

flag() {
    local v
    v="$(cfg_get "$1" 2>/dev/null || true)"
    [[ "${v,,}" == "true" ]]
}

# Write a file atomically with the given mode and group.
put() {
    local path="$1" mode="$2" group="$3" tmp
    tmp="$(mktemp "${path}.XXXXXX")"
    cat >"$tmp"
    chmod "$mode" "$tmp"
    chgrp "$group" "$tmp" 2>/dev/null || true
    mv -f "$tmp" "$path"
}

write_include() {
    put "$INCLUDE" 0644 root <<EOF
# Managed by scripts/web_auth.sh — do not edit; Settings -> System -> Web login.
satisfy any;
allow 127.0.0.1;
allow ::1;
deny all;
auth_basic "${REALM}";
auth_basic_user_file ${HTPASSWD};
EOF
}

# Test the new config; put the old include back if nginx refuses it.
reload_nginx() {
    local backup="$1"
    command -v nginx >/dev/null 2>&1 || return 0
    if ! nginx -t >/dev/null 2>&1; then
        if [[ -n "$backup" && -f "$backup" ]]; then mv -f "$backup" "$INCLUDE"; else rm -f "$INCLUDE"; fi
        die "nginx rejected the configuration — web login left as it was"
    fi
    rm -f "$backup" 2>/dev/null || true
    systemctl reload nginx.service 2>/dev/null || systemctl restart nginx.service
}

backup_include() {
    if [[ -f "$INCLUDE" ]]; then
        local b
        b="$(mktemp "${INCLUDE}.bak.XXXXXX")"
        cp -p "$INCLUDE" "$b"
        echo "$b"
    fi
}

cmd_status() {
    local enabled=false user default=false
    [[ -f "$INCLUDE" && -f "$HTPASSWD" ]] && enabled=true
    user="$(current_user)"
    flag auth.default_password && default=true
    printf '{"enabled": %s, "username": "%s", "default_password": %s}\n' \
        "$enabled" "${user:-$DEFAULT_USER}" "$default"
}

cmd_enable() {
    local user="${1:-}" pass hash existing
    [[ $EUID -eq 0 ]] || die "run as root"
    command -v openssl >/dev/null 2>&1 || die "openssl is required to hash the password"
    existing="$(current_user)"
    user="${user:-${existing:-$DEFAULT_USER}}"
    valid_user "$user" || die "username: 1-32 letters, digits, . _ or -"
    # The password arrives on stdin (empty = none given). Never as an argument.
    pass=""
    if [[ ! -t 0 ]]; then IFS= read -r pass || true; fi
    [[ "$pass" != *$'\n'* ]] || die "password must be one line"
    (( ${#pass} <= 128 )) || die "password: at most 128 characters"

    if [[ -n "$pass" ]]; then
        hash="$(printf '%s' "$pass" | openssl passwd -6 -stdin)"
        cfg_set auth.default_password false
    elif [[ -n "$existing" ]]; then
        # Rename only: an htpasswd line is user:hash, so the hash carries over.
        hash="$(head -1 "$HTPASSWD" | cut -d: -f2-)"
    else
        hash="$(printf '%s' "$DEFAULT_PASS" | openssl passwd -6 -stdin)"
        cfg_set auth.default_password true
    fi
    [[ "$hash" == \$6\$* ]] || die "could not hash the password"

    local backup
    backup="$(backup_include)"
    printf '%s:%s\n' "$user" "$hash" | put "$HTPASSWD" 0640 www-data
    write_include
    cfg_set auth.enabled true
    cfg_set auth.username "$user"
    reload_nginx "$backup"
    log "web login on for user '${user}'$(flag auth.default_password && echo ' (default password)')"
    cmd_status
}

cmd_disable() {
    [[ $EUID -eq 0 ]] || die "run as root"
    local backup
    backup="$(backup_include)"
    rm -f "$INCLUDE"
    cfg_set auth.enabled false
    reload_nginx "$backup"
    log "web login off (the password file is kept for next time)"
    cmd_status
}

# install.sh: make nginx match player.yaml (a fresh image, or a restored config).
cmd_apply() {
    [[ $EUID -eq 0 ]] || die "run as root"
    if flag auth.enabled; then
        if [[ -f "$HTPASSWD" ]]; then
            local backup
            backup="$(backup_include)"
            write_include
            reload_nginx "$backup"
        else
            cmd_enable "$(cfg_get auth.username 2>/dev/null || true)" </dev/null >/dev/null
        fi
        log "web login is on"
    elif [[ -f "$INCLUDE" ]]; then
        cmd_disable >/dev/null
    fi
}

case "${1:-status}" in
    status) cmd_status ;;
    enable) shift; cmd_enable "${1:-}" ;;
    disable) cmd_disable ;;
    apply) cmd_apply ;;
    *) die "usage: web_auth.sh status|enable [USER] (password on stdin)|disable|apply" ;;
esac
