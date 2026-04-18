#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# vybe-camera-agent — remote update helper.
#
# Pulls the latest code, rebuilds the agent image, and rolls the compose stack
# with zero disruption beyond the brief agent restart. dnsmasq and tailscale
# survive the update because they only restart when their image changes.
#
# Typical usage (after SSHing in via Tailscale):
#   ssh root@vybe-camera-agent
#   cd ~/vybe/vybe-camera-agent   # or wherever the repo is checked out
#   sudo ./update.sh              # accepts: --no-pull, --restart-all
#
# Flags:
#   --no-pull      Skip `git pull` — useful when you already synced the repo.
#   --restart-all  Recreate every service, not just the agent (rare).
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="docker-compose.site.yml"

DO_PULL=1
RESTART_ALL=0
for arg in "$@"; do
  case "${arg}" in
    --no-pull)      DO_PULL=0 ;;
    --restart-all)  RESTART_ALL=1 ;;
    -h|--help)
      sed -n '1,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//; /^#!/d; /^set -euo/d'
      exit 0 ;;
    *)
      echo "update.sh: unknown flag '${arg}'" >&2
      exit 1 ;;
  esac
done

cd "${SCRIPT_DIR}"

# ---------------------------------------------------------------------------

require_compose() {
  if ! docker compose version >/dev/null 2>&1; then
    echo "update.sh: 'docker compose' is not available — run install.sh first" >&2
    exit 1
  fi
  if [[ ! -f "${COMPOSE_FILE}" ]]; then
    echo "update.sh: ${COMPOSE_FILE} not found in ${SCRIPT_DIR}" >&2
    exit 1
  fi
  if [[ ! -f .env ]]; then
    echo "update.sh: .env not found — run install.sh to generate it" >&2
    exit 1
  fi
}

git_pull() {
  [[ ${DO_PULL} -eq 1 ]] || { echo "==> Skipping git pull (per flag)"; return; }
  if [[ ! -d .git && ! -d ../.git ]]; then
    echo "==> Not a git checkout — skipping git pull"
    return
  fi
  echo "==> Pulling latest"
  # Update from the repo root — the agent lives in a subfolder of the monorepo.
  local repo_root
  repo_root="$(git -C "${SCRIPT_DIR}" rev-parse --show-toplevel)"
  git -C "${repo_root}" fetch --prune
  git -C "${repo_root}" pull --ff-only
}

rebuild_and_roll() {
  echo "==> Rebuilding agent image"
  docker compose -f "${COMPOSE_FILE}" build agent

  if [[ ${RESTART_ALL} -eq 1 ]]; then
    echo "==> Recreating every service (--restart-all)"
    docker compose -f "${COMPOSE_FILE}" up -d --force-recreate
  else
    echo "==> Rolling the agent container"
    docker compose -f "${COMPOSE_FILE}" up -d --no-deps agent
  fi
}

prune_old_images() {
  echo "==> Pruning dangling images"
  docker image prune -f >/dev/null
}

show_status() {
  echo
  echo "==> Post-update status:"
  docker compose -f "${COMPOSE_FILE}" ps
  echo
  echo "    Tail agent logs:   docker compose -f ${COMPOSE_FILE} logs -f agent"
}

# ---------------------------------------------------------------------------

main() {
  require_compose
  git_pull
  rebuild_and_roll
  prune_old_images
  show_status
}

main "$@"
