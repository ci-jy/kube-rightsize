#!/usr/bin/env bash
# Start or stop a private Docker daemon whose data lives under $TMPDIR, for
# hosts where the system daemon is not usable (no docker group, or a sandbox
# where runc trips over sysctls). Uses crun when it is installed.
#
#   eval "$(scripts/dockerd.sh start)"   # exports DOCKER_HOST
#   scripts/dockerd.sh stop
#
# Needs sudo. The daemon gets its own bridge-less setup and address pool so it
# does not collide with a system Docker on the same machine.
set -euo pipefail
root=${RIGHTSIZE_DOCKER_ROOT:-${TMPDIR:-/tmp}/rightsize-docker}
sock="$root/docker.sock"
pidfile="$root/docker.pid"

case "${1:-}" in
  start)
    mkdir -p "$root"
    if ! sudo test -S "$sock" || ! sudo env DOCKER_HOST="unix://$sock" docker info >/dev/null 2>&1; then
      runtime=()
      if command -v crun >/dev/null; then
        runtime=(--add-runtime "crun=$(command -v crun)" --default-runtime crun)
      fi
      sudo dockerd \
        --data-root "$root/data" --exec-root "$root/exec" --pidfile "$pidfile" \
        --host "unix://$sock" --group "$(id -gn)" --bridge none \
        --default-address-pool base=10.213.0.0/16,size=24 \
        "${runtime[@]}" >"$root/dockerd.log" 2>&1 &
      for _ in $(seq 60); do
        DOCKER_HOST="unix://$sock" docker info >/dev/null 2>&1 && break
        sleep 1
      done
      DOCKER_HOST="unix://$sock" docker info >/dev/null 2>&1 || {
        tail -20 "$root/dockerd.log" >&2
        exit 1
      }
    fi
    echo "export DOCKER_HOST=unix://$sock"
    ;;
  stop)
    if sudo test -f "$pidfile"; then
      pid=$(sudo cat "$pidfile")
      sudo kill "$pid" 2>/dev/null || true
      for _ in $(seq 30); do sudo kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    fi
    ;;
  *)
    echo "usage: dockerd.sh start|stop" >&2
    exit 2
    ;;
esac
