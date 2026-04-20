#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# vybe-camera-agent — on-site host bootstrap.
#
# Configures an Ubuntu/Debian mini-PC as a DHCP+NAT router for a camera LAN
# and starts the agent compose stack.
#
# Requires: root, Ubuntu/Debian with netplan, 2 physical NICs.
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAN_ADDRESS="10.20.0.1/24"
LAN_CIDR="10.20.0.0/24"
NETPLAN_LAN_FILE="/etc/netplan/99-vybe-lan.yaml"
NETPLAN_WIFI_FILE="/etc/netplan/98-vybe-wifi.yaml"
SYSCTL_FILE="/etc/sysctl.d/99-vybe-forward.conf"

# Populated by prompt_wifi_networks; indexed arrays stay in sync.
WIFI_IF=""
WIFI_SSIDS=()
WIFI_PASSWORDS=()

# ---------------------------------------------------------------------------

require_root() {
  if [[ $EUID -ne 0 ]]; then
    echo "install.sh: must be run as root (try: sudo $0)" >&2
    exit 1
  fi
}

require_debian_like() {
  if ! command -v apt-get >/dev/null 2>&1; then
    echo "install.sh: apt-get not found — this script supports Ubuntu/Debian only" >&2
    exit 1
  fi
  if ! command -v netplan >/dev/null 2>&1; then
    echo "install.sh: netplan not found — install netplan.io or configure the LAN NIC manually" >&2
    exit 1
  fi
}

install_packages() {
  echo "==> Installing host packages"
  DEBIAN_FRONTEND=noninteractive apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    iptables \
    iptables-persistent \
    v4l-utils \
    wpasupplicant \
    ca-certificates \
    curl
  if ! command -v docker >/dev/null 2>&1; then
    echo "==> Installing Docker"
    curl -fsSL https://get.docker.com | sh
  fi
  if ! docker compose version >/dev/null 2>&1; then
    # get.docker.com installs the plugin on modern distros, but fall back if missing.
    DEBIAN_FRONTEND=noninteractive apt-get install -y docker-compose-plugin || true
  fi
}

list_nics() {
  ip -br link show | awk '$1 != "lo" {print $1, $3}'
}

prompt_nics() {
  echo
  echo "==> Available network interfaces:"
  list_nics | awk '{printf "    %-16s %s\n", $1, $2}'
  echo
  read -r -p "Which NIC faces the internet (WAN)? " WAN_IF
  read -r -p "Which NIC is dedicated to the camera LAN? " LAN_IF
  if [[ -z "${WAN_IF}" || -z "${LAN_IF}" || "${WAN_IF}" == "${LAN_IF}" ]]; then
    echo "install.sh: WAN_IF and LAN_IF must be different non-empty interface names" >&2
    exit 1
  fi
  if ! ip link show "${WAN_IF}" >/dev/null 2>&1; then
    echo "install.sh: no such interface: ${WAN_IF}" >&2; exit 1
  fi
  if ! ip link show "${LAN_IF}" >/dev/null 2>&1; then
    echo "install.sh: no such interface: ${LAN_IF}" >&2; exit 1
  fi
}

write_netplan() {
  echo "==> Writing ${NETPLAN_LAN_FILE} (static ${LAN_ADDRESS} on ${LAN_IF})"
  cat > "${NETPLAN_LAN_FILE}" <<EOF
# Managed by vybe-camera-agent install.sh
network:
  version: 2
  ethernets:
    ${LAN_IF}:
      dhcp4: no
      dhcp6: no
      addresses:
        - ${LAN_ADDRESS}
      link-local: []
EOF
  chmod 600 "${NETPLAN_LAN_FILE}"
  netplan apply
}

detect_wifi_interface() {
  local iface
  for iface in /sys/class/net/*/wireless; do
    [[ -d "${iface}" ]] || continue
    basename "$(dirname "${iface}")"
    return 0
  done
  return 1
}

prompt_wifi_networks() {
  echo
  local detected
  if detected="$(detect_wifi_interface)"; then
    echo "==> Detected wifi interface: ${detected}"
    read -r -p "Configure wifi connections for this host? [y/N]: " reply
  else
    echo "==> No wifi interface detected."
    read -r -p "Still want to write a wifi config? [y/N]: " reply
  fi

  case "${reply,,}" in
    y|yes) ;;
    *) echo "    Skipping wifi configuration."; return ;;
  esac

  read -r -p "Wifi interface name [${detected:-wlan0}]: " WIFI_IF
  WIFI_IF="${WIFI_IF:-${detected:-wlan0}}"
  if ! ip link show "${WIFI_IF}" >/dev/null 2>&1; then
    echo "install.sh: no such interface: ${WIFI_IF}" >&2
    WIFI_IF=""
    return
  fi

  echo
  echo "==> Add known wifi networks (the host will auto-connect to whichever is in range)."
  echo "    Press Enter with empty SSID when done. Repeating an SSID updates its password."
  while :; do
    local ssid password
    read -r -p "  SSID (blank to finish): " ssid
    [[ -z "${ssid}" ]] && break
    read -r -s -p "  Password for '${ssid}' (blank for open network): " password; echo
    # Replace any previous entry with the same SSID so netplan doesn't fail on duplicates.
    local i found=""
    for i in "${!WIFI_SSIDS[@]}"; do
      if [[ "${WIFI_SSIDS[$i]}" == "${ssid}" ]]; then
        WIFI_PASSWORDS[$i]="${password}"
        found=1
        echo "    (updated password for existing SSID '${ssid}')"
        break
      fi
    done
    if [[ -z "${found}" ]]; then
      WIFI_SSIDS+=("${ssid}")
      WIFI_PASSWORDS+=("${password}")
    fi
  done

  if [[ ${#WIFI_SSIDS[@]} -eq 0 ]]; then
    echo "    No SSIDs entered — skipping wifi config."
    WIFI_IF=""
  fi
}

write_wifi_netplan() {
  if [[ -z "${WIFI_IF}" || ${#WIFI_SSIDS[@]} -eq 0 ]]; then
    # Keep any previously-written wifi config in place but don't touch it.
    return
  fi

  echo "==> Writing ${NETPLAN_WIFI_FILE} (${#WIFI_SSIDS[@]} network(s) on ${WIFI_IF})"
  {
    echo "# Managed by vybe-camera-agent install.sh"
    echo "network:"
    echo "  version: 2"
    echo "  wifis:"
    echo "    ${WIFI_IF}:"
    echo "      dhcp4: yes"
    echo "      dhcp6: no"
    echo "      access-points:"
    local i
    for i in "${!WIFI_SSIDS[@]}"; do
      local ssid="${WIFI_SSIDS[$i]}"
      local password="${WIFI_PASSWORDS[$i]}"
      # YAML-quote the SSID (it may contain spaces or special chars)
      local ssid_q
      ssid_q="\"$(printf '%s' "${ssid}" | sed 's/\\/\\\\/g; s/"/\\"/g')\""
      echo "        ${ssid_q}:"
      if [[ -n "${password}" ]]; then
        local pw_q
        pw_q="\"$(printf '%s' "${password}" | sed 's/\\/\\\\/g; s/"/\\"/g')\""
        echo "          password: ${pw_q}"
      fi
    done
  } > "${NETPLAN_WIFI_FILE}"
  chmod 600 "${NETPLAN_WIFI_FILE}"
  netplan apply
}

enable_ip_forward() {
  echo "==> Enabling net.ipv4.ip_forward"
  cat > "${SYSCTL_FILE}" <<EOF
# Managed by vybe-camera-agent install.sh
net.ipv4.ip_forward = 1
EOF
  sysctl -p "${SYSCTL_FILE}" >/dev/null
}

prompt_agent_env() {
  echo
  echo "==> Agent backend settings"
  echo "    (paste values from the b2b 'Generate Credential' dialog for this venue)"
  read -r -p "API base URL [https://d16rcatmaudft5.cloudfront.net]: " API_BASE_URL
  API_BASE_URL="${API_BASE_URL:-https://d16rcatmaudft5.cloudfront.net}"
  read -r -p "Venue ID: " VENUE_ID
  read -r -p "Keycloak URL [${API_BASE_URL}/auth]: " KEYCLOAK_URL
  KEYCLOAK_URL="${KEYCLOAK_URL:-${API_BASE_URL}/auth}"
  read -r -p "Keycloak realm [vybe]: " KEYCLOAK_REALM
  KEYCLOAK_REALM="${KEYCLOAK_REALM:-vybe}"
  read -r -p "Keycloak client ID (camera-agent-<uuid>-<ts>): " KEYCLOAK_CLIENT_ID
  read -r -s -p "Keycloak client secret: " KEYCLOAK_CLIENT_SECRET; echo

  echo
  echo "==> Tailscale"
  echo "    Mint a reusable auth key: https://login.tailscale.com/admin/settings/keys"
  read -r -p "Tailscale auth key: " TS_AUTHKEY
  read -r -p "Tailscale hostname [vybe-camera-${VENUE_ID:0:8}]: " TS_HOSTNAME
  TS_HOSTNAME="${TS_HOSTNAME:-vybe-camera-${VENUE_ID:0:8}}"
}

render_env() {
  echo "==> Writing ${SCRIPT_DIR}/.env"
  cat > "${SCRIPT_DIR}/.env" <<EOF
# Generated by install.sh — re-run install.sh to regenerate.
WAN_IF=${WAN_IF}
LAN_IF=${LAN_IF}
LAN_CIDR=${LAN_CIDR}

TS_AUTHKEY=${TS_AUTHKEY}
TS_HOSTNAME=${TS_HOSTNAME}
TS_EXTRA_ARGS=--ssh --accept-dns=false

API_BASE_URL=${API_BASE_URL}
VENUE_ID=${VENUE_ID}
KEYCLOAK_URL=${KEYCLOAK_URL}
KEYCLOAK_REALM=${KEYCLOAK_REALM}
KEYCLOAK_CLIENT_ID=${KEYCLOAK_CLIENT_ID}
KEYCLOAK_CLIENT_SECRET=${KEYCLOAK_CLIENT_SECRET}

OUTPUT_DIR=/output
EOF
  chmod 600 "${SCRIPT_DIR}/.env"
}

seed_config_yaml() {
  local target="${SCRIPT_DIR}/config.yaml"
  if [[ -f "${target}" ]]; then
    echo "==> ${target} already exists — leaving untouched"
    return
  fi
  echo "==> Seeding ${target}"
  cat > "${target}" <<EOF
# Generated by install.sh — edit via the web dashboard or in place.
chunk_duration_seconds: 30
temp_dir: /tmp/vybe-camera-agent
web_port: 5174
storage_mode: upload
output_dir: \${OUTPUT_DIR}
venue_id: \${VENUE_ID}
api_base_url: \${API_BASE_URL}
keycloak_url: \${KEYCLOAK_URL}
keycloak_realm: \${KEYCLOAK_REALM}
keycloak_client_id: \${KEYCLOAK_CLIENT_ID}
keycloak_client_secret: \${KEYCLOAK_CLIENT_SECRET}

lan_discovery:
  enabled: true
  leases_file: /var/lib/dnsmasq/dnsmasq.leases
  rtsp_port: 554

cameras: []
EOF
}

start_compose() {
  echo "==> Building + starting compose stack"
  cd "${SCRIPT_DIR}"
  docker compose -f docker-compose.site.yml pull || true
  docker compose -f docker-compose.site.yml up -d --build
}

print_next_steps() {
  echo
  echo "================================================================="
  echo "  vybe-camera-agent is up."
  echo "  Local dashboard:  http://$(echo "${LAN_ADDRESS}" | cut -d/ -f1):5174"
  echo "  Tailscale:        http://${TS_HOSTNAME}:5174"
  echo "  (Tailscale may take ~30s to bring up the node on first start.)"
  echo "================================================================="
  echo
  echo "Next steps:"
  echo "  1. Plug IP cameras into the ${LAN_IF} interface. They should get"
  echo "     DHCP leases in the 10.20.0.100-10.20.0.250 range and appear in"
  echo "     the 'Discovered Cameras' section of the dashboard."
  echo "  2. Enter the RTSP username/password in the dashboard to start capture."
  echo "  3. For USB cameras, click 'Scan USB' and add from the listing."
  echo
}

# ---------------------------------------------------------------------------

main() {
  require_root
  require_debian_like
  install_packages
  prompt_nics
  prompt_wifi_networks
  write_netplan
  write_wifi_netplan
  enable_ip_forward
  prompt_agent_env
  render_env
  seed_config_yaml
  start_compose
  print_next_steps
}

main "$@"
