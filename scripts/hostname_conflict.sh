#!/usr/bin/env bash
# Keep two SAFYDO boxes from fighting over the same .local name.
#
# Avahi already resolves collisions on its own, but it does it by appending a
# counter in whatever order the boards happen to boot — so the same box is
# safydo.local today and safydo-2.local tomorrow, and you cannot tell which is
# which. This picks a name that is derived from something you can see:
#
#     safydo.local  +  a second box at 10.0.0.119  ->  safydo-119.local
#
# The suffix is the last octet of the box's own IPv4 address, which is unique
# within a subnet, so two boards can never choose the same name.
#
# CAVEAT, deliberately not hidden: with DHCP the last octet can change after a
# lease expires, and then the mDNS name changes with it. That is why the rename
# only ever fires when a real conflict is detected, and why a DHCP reservation
# (or SAFYDO_HOSTNAME= at install time) is the better answer for a box you want
# to bookmark. Run 'status' any time to see the current name and reasoning.
#
# Usage:
#   hostname_conflict.sh status     # report, change nothing
#   hostname_conflict.sh apply      # rename this box if it is the newcomer
#   hostname_conflict.sh reclaim    # take the base name back (manual, checked)
#   hostname_conflict.sh set NAME [--force]
#                                   # rename to a name you chose (Settings ->
#                                   # General). Refused if another device on
#                                   # the LAN answers NAME.local, unless --force.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_common.sh
source "${SCRIPT_DIR}/_common.sh"

BASE_NAME="${SAFYDO_BASE_HOSTNAME:-safydo}"
STATE_FILE="${STATE_DIR}/hostname_claim.json"
RESOLVE_TRIES="${RESOLVE_TRIES:-2}"

# Every global IPv4 this box currently holds.
own_ipv4s() {
    ip -4 -o addr show scope global 2>/dev/null | awk '{split($4,a,"/"); print a[1]}'
}

# The address we would be reached on: the one carrying the default route.
primary_ipv4() {
    local dev addr=""
    # By the "dev" keyword (_common.sh), not field 5, which is only the device
    # in the "default via X dev Y" shape.
    dev="$(default_route_iface)"
    if [[ -n "$dev" ]]; then
        addr="$(ip -4 -o addr show dev "$dev" scope global 2>/dev/null \
                | awk '{split($4,a,"/"); print a[1]; exit}')"
    fi
    # Key the fallback on the address, not the device name: at boot the default
    # route can exist before DHCP has given that device an address, and gating
    # on "$dev is empty" skipped the fallback in exactly that window.
    [[ -n "$addr" ]] || addr="$(own_ipv4s | head -1)"
    echo "$addr"
}


is_own_ip() {
    local target="$1" ip
    while read -r ip; do
        [[ "$ip" == "$target" ]] && return 0
    done < <(own_ipv4s)
    return 1
}

# Find ANOTHER box claiming NAME.local. Prints its IPv4, or nothing.
#
# Two traps this has to avoid, both observed on real hardware:
#   1. Self-masking — `avahi-resolve safydo.local` on a box that publishes that
#      name answers from its own record, so an incumbent peer is invisible.
#   2. Silent collisions — two boards held safydo.local simultaneously for
#      hours and neither avahi renamed itself, because the switch was not
#      passing 224.0.0.251 between them. Multicast cannot be relied on.
# So: ask the neighbours directly, over unicast, and never ask ourselves.
other_holder() {
    local name="$1" i out ip
    for ((i = 0; i < RESOLVE_TRIES; i++)); do
        out="$(timeout 15 python3 "${SCRIPT_DIR}/mdns_probe.py" "$name" 2>/dev/null | awk '{print $1; exit}')"
        if [[ "$out" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] && ! is_own_ip "$out"; then
            echo "$out"
            return 0
        fi
        # Multicast still worth a try: it finds boxes not in our ARP table.
        if command -v avahi-resolve >/dev/null 2>&1; then
            ip="$(timeout 4 avahi-resolve -4 -n "${name}.local" 2>/dev/null | awk '{print $2; exit}')"
            if [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] && ! is_own_ip "$ip"; then
                echo "$ip"
                return 0
            fi
        fi
        sleep 2
    done
}

# safydo -> safydo-119
suffixed_name() {
    local ip="$1"
    echo "${BASE_NAME}-${ip##*.}"
}

apply_hostname() {
    local new="$1" old
    old="$(hostname)"
    [[ "$new" == "$old" ]] && return 0

    hostnamectl set-hostname "$new" 2>/dev/null || echo "$new" > /etc/hostname
    # Keep 127.0.1.1 in step or sudo warns and some tools stall on lookup.
    if [[ -f /etc/hosts ]]; then
        if grep -qE '^127\.0\.1\.1' /etc/hosts; then
            sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t${new}.local\t${new}/" /etc/hosts
        else
            printf '127.0.1.1\t%s.local\t%s\n' "$new" "$new" >> /etc/hosts
        fi
    fi
    # The UI reads these, so a stale value would show the wrong address.
    cfg_set "hostname" "$new" 2>/dev/null || true
    cfg_set "mdns_name" "${new}.local" 2>/dev/null || true

    systemctl restart avahi-daemon 2>/dev/null || true

    mkdir -p "$STATE_DIR" 2>/dev/null || true
    printf '{"hostname":"%s","base":"%s","reason":"%s","changed_at":"%s"}\n' \
        "$new" "$BASE_NAME" "${2:-mdns-conflict}" "$(date -Is)" > "$STATE_FILE" 2>/dev/null || true

    log "renamed ${old} -> ${new} (${new}.local)"
}

# Decide the name this box should answer to. Echoes it; empty = leave alone.
decide() {
    local current holder ip want generated=false

    current="$(hostname)"
    # Eligibility first: probing costs seconds and this runs at boot. Only two
    # kinds of box can ever be renamed here — one still on the base name, and
    # one carrying a suffix WE generated whose IP may since have moved. A
    # deliberately chosen name (SAFYDO_HOSTNAME=kitchen) is neither, so it must
    # not pay for a probe whose answer it could never act on.
    # A name set in Settings (cmd_set, reason "user") also writes STATE_FILE,
    # and "safydo-42" looks exactly like a suffix we generated: without the
    # reason check the next boot would "fix" it to the current IP's suffix.
    if [[ -f "$STATE_FILE" && "$current" =~ ^${BASE_NAME}-[0-9]+$ ]] \
            && ! grep -q '"reason":"user"' "$STATE_FILE" 2>/dev/null; then
        generated=true
    fi
    if [[ "$current" != "$BASE_NAME" ]] && ! $generated; then
        log "hostname ${current} was chosen deliberately — leaving it alone"
        return 0
    fi

    ip="$(primary_ipv4)"
    if [[ -z "$ip" ]]; then
        log "no IPv4 yet — cannot judge a conflict, leaving ${current} alone"
        return 0
    fi

    holder="$(other_holder "$BASE_NAME")"

    # Nobody answered. That is NOT proof the name is free: a peer can be slow
    # to announce or briefly unreachable. Reclaiming on silence is how this
    # script once renamed itself back into the collision it had just resolved,
    # so a box holding a suffixed name keeps it. Use 'reclaim' to move back.
    [[ -n "$holder" ]] || return 0

    want="$(suffixed_name "$ip")"
    if $generated; then
        [[ "$current" == "$want" ]] && return 0
        log "IP changed — suffix ${current} is stale"
        echo "$want"
        return 0
    fi

    log "${BASE_NAME}.local is already held by ${holder} — this box is the newcomer"
    echo "$want"
}


cmd_status() {
    local holder ip
    ip="$(primary_ipv4)"
    holder="$(other_holder "$BASE_NAME")"
    echo "hostname   : $(hostname)"
    echo "base name  : ${BASE_NAME}"
    echo "own IPv4   : ${ip:-none}"
    if [[ -z "$holder" ]]; then
        echo "${BASE_NAME}.local : no other box claims it"
    else
        echo "${BASE_NAME}.local : ${holder} (ANOTHER box)"
        echo "would rename to    : $(suffixed_name "${ip:-0.0.0.0}")"
    fi
    [[ -f "$STATE_FILE" ]] && echo "claim state: $(cat "$STATE_FILE")"
    return 0
}

# RFC 1123 label, lower case: what mDNS, DHCP and every browser accept as a
# .local host. Kept in step with backend/services/system.py valid_hostname().
valid_hostname() {
    local n="$1"
    [[ "$n" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]] || return 1
    [[ "$n" =~ ^[0-9]+$ ]] && return 1          # all digits reads as an address
    [[ "$n" == "localhost" ]] && return 1
    return 0
}

cmd_set() {
    local new="${1:-}" force="${2:-}" holder
    [[ "${EUID}" -eq 0 ]] || die "run as root: sudo $0 set NAME"
    new="${new,,}"
    valid_hostname "$new" || die "invalid name '${1:-}': use a-z, 0-9 and '-', 1-63 characters, not starting or ending with '-'"
    [[ "$new" == "$(hostname)" ]] && { log "already ${new}"; return 0; }
    if [[ "$force" != "--force" ]]; then
        holder="$(other_holder "$new")"
        [[ -z "$holder" ]] || die "${new}.local is already answered by ${holder} — pick another name"
    fi
    # Recorded as the user's choice: decide() leaves any name other than the
    # base (or a suffix it generated) alone, so boot-time conflict handling
    # never undoes this.
    apply_hostname "$new" "user"
}

main() {
    case "${1:-status}" in
        status) cmd_status ;;
        apply)
            [[ "${EUID}" -eq 0 ]] || die "run as root: sudo $0 apply"
            local new
            new="$(decide)"
            if [[ -n "$new" ]]; then
                apply_hostname "$new"
            else
                log "hostname $(hostname) is fine — no change"
            fi
            ;;
        reclaim)
            [[ "${EUID}" -eq 0 ]] || die "run as root: sudo $0 reclaim"
            local holder
            holder="$(other_holder "$BASE_NAME")"
            [[ -z "$holder" ]] || die "${BASE_NAME}.local is held by ${holder} — not reclaiming"
            apply_hostname "$BASE_NAME"
            rm -f "$STATE_FILE"
            ;;
        set) shift; cmd_set "$@" ;;
        -h|--help) sed -n '2,26p' "$0" ;;
        *) die "unknown command: $1 (status|apply|reclaim|set)" ;;
    esac
}

main "$@"
