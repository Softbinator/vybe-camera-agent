#!/bin/sh
# Idempotent NAT setup for the camera LAN.
# Runs once at compose startup (restart: "no") and exits.
#
# Required env vars:
#   WAN_IF    — interface facing the internet (e.g. eth0)
#   LAN_CIDR  — camera-LAN CIDR (e.g. 10.20.0.0/24)
#
# This script runs inside an alpine container with network_mode: host and
# cap_add: NET_ADMIN, so iptables rules we install here are on the host's
# default namespace.

set -eu

: "${WAN_IF:?WAN_IF must be set (e.g. eth0)}"
: "${LAN_CIDR:=10.20.0.0/24}"

echo "nat-setup: WAN_IF=${WAN_IF} LAN_CIDR=${LAN_CIDR}"

# Install iptables in the container if missing
if ! command -v iptables >/dev/null 2>&1; then
  apk add --no-cache iptables >/dev/null
fi

# Enable IPv4 forwarding in the host kernel (shared via host network namespace).
# Use sysctl rather than writing to /proc directly — /proc/sys is read-only
# inside containers even with NET_ADMIN.
sysctl -w net.ipv4.ip_forward=1

# MASQUERADE outbound traffic from the camera LAN through the WAN interface
if ! iptables -t nat -C POSTROUTING -s "${LAN_CIDR}" -o "${WAN_IF}" -j MASQUERADE 2>/dev/null; then
  iptables -t nat -A POSTROUTING -s "${LAN_CIDR}" -o "${WAN_IF}" -j MASQUERADE
  echo "nat-setup: added MASQUERADE rule for ${LAN_CIDR} out via ${WAN_IF}"
else
  echo "nat-setup: MASQUERADE rule already present — nothing to do"
fi

# Allow established/related reply traffic back in, and new LAN-initiated traffic out.
# Skip if a blanket ACCEPT FORWARD policy is already in place.
if [ "$(iptables -P FORWARD 2>/dev/null; iptables -L FORWARD -n | head -1)" != "Chain FORWARD (policy ACCEPT)" ]; then
  iptables -C FORWARD -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null \
    || iptables -A FORWARD -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
  iptables -C FORWARD -s "${LAN_CIDR}" -j ACCEPT 2>/dev/null \
    || iptables -A FORWARD -s "${LAN_CIDR}" -j ACCEPT
fi

echo "nat-setup: done."
