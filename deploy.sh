#!/usr/bin/env bash
# Plex-qBittorrent Speed Manager - deployment helper.
# Pulls the published GHCR image and brings the compose stack up.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

NETWORK_NAME="plex-network"
PORT="${HTTP_PORT:-5252}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log_info()    { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[OK]${NC} $1"; }
log_warning() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $1"; }

# docker compose (v2) with a fallback to docker-compose (v1)
if docker compose version &>/dev/null; then
    COMPOSE=(docker compose)
elif command -v docker-compose &>/dev/null; then
    COMPOSE=(docker-compose)
else
    log_error "Neither 'docker compose' nor 'docker-compose' is available"
    exit 1
fi

check_docker() {
    command -v docker &>/dev/null || { log_error "Docker is not installed or not in PATH"; exit 1; }
    docker info &>/dev/null || { log_error "Docker daemon is not running"; exit 1; }
    log_success "Docker is available"
}

check_env_file() {
    if [[ ! -f .env ]]; then
        if [[ -f .env.example ]]; then
            cp .env.example .env
            log_warning "Created .env from .env.example"
            log_error "Edit .env with your actual credentials, then re-run this script"
        else
            log_error ".env not found and no .env.example to copy from"
        fi
        exit 1
    fi

    local missing=()
    for var in PLEX_TOKEN QBITTORRENT_USERNAME QBITTORRENT_PASSWORD; do
        if ! grep -q "^${var}=." .env || grep -q "^${var}=.*your_.*_here" .env; then
            missing+=("$var")
        fi
    done

    if (( ${#missing[@]} > 0 )); then
        log_error "Missing or placeholder values in .env:"
        printf '  - %s\n' "${missing[@]}"
        exit 1
    fi

    log_success "Environment file validated"
}

create_network() {
    if docker network inspect "$NETWORK_NAME" &>/dev/null; then
        log_info "Network '$NETWORK_NAME' already exists"
    else
        docker network create "$NETWORK_NAME" >/dev/null
        log_success "Created network '$NETWORK_NAME'"
    fi
}

deploy_stack() {
    log_info "Pulling image..."
    "${COMPOSE[@]}" pull
    log_info "Starting stack..."
    "${COMPOSE[@]}" up -d
    log_success "Stack deployed"
}

wait_for_health() {
    log_info "Waiting for the service to become healthy..."
    for _ in $(seq 1 30); do
        if curl -f -s "http://localhost:${PORT}/health" >/dev/null 2>&1; then
            echo
            log_success "Service is healthy"
            return 0
        fi
        printf '.'
        sleep 2
    done
    echo
    log_error "Service did not become healthy within 60s"
    log_info "Inspect with: ${COMPOSE[*]} logs -f"
    return 1
}

show_status() {
    "${COMPOSE[@]}" ps
    echo
    curl -s "http://localhost:${PORT}/status" | python3 -m json.tool 2>/dev/null \
        || log_warning "Could not retrieve /status"
}

show_next_steps() {
    local ip
    ip="$(hostname -I 2>/dev/null | awk '{print $1}')" || ip="YOUR_SERVER_IP"
    cat <<EOF

Next steps:
  1. Add the Plex webhook (Settings -> Network -> Webhooks, Plex Pass required):
       http://${ip:-YOUR_SERVER_IP}:${PORT}/webhook
  2. Follow the logs:
       ${COMPOSE[*]} logs -f
  3. Check state at any time:
       curl http://localhost:${PORT}/status
EOF
}

case "${1:-deploy}" in
    deploy)
        echo "Plex-qBittorrent Speed Manager - deploy"
        echo "======================================="
        check_docker
        check_env_file
        create_network
        deploy_stack
        if wait_for_health; then
            show_status
            show_next_steps
        else
            "${COMPOSE[@]}" logs --tail 50
            exit 1
        fi
        ;;
    update)
        log_info "Pulling latest image and recreating..."
        "${COMPOSE[@]}" pull
        "${COMPOSE[@]}" up -d
        wait_for_health
        ;;
    stop)    "${COMPOSE[@]}" down && log_success "Stack stopped" ;;
    restart) "${COMPOSE[@]}" restart && wait_for_health ;;
    logs)    "${COMPOSE[@]}" logs -f --tail 100 ;;
    status)  show_status ;;
    test)    python3 test_integration.py "http://localhost:${PORT}" ;;
    clean)
        "${COMPOSE[@]}" down -v --remove-orphans
        log_success "Cleanup complete"
        ;;
    *)
        cat <<EOF
Usage: $0 [command]

  deploy   Pull the image and start the stack (default)
  update   Pull a newer image and recreate the container
  stop     Stop and remove the stack
  restart  Restart the container
  logs     Follow logs
  status   Show container and application status
  test     Run integration tests
  clean    Remove the stack and its volumes
EOF
        exit 1
        ;;
esac
