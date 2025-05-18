#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  ./start-mariadb.sh <ROOT_PASSWORD> [CONTAINER_NAME] [HOST_PORT]

Examples:
  ./start-mariadb.sh 'S3cureP@ss!'
  ./start-mariadb.sh 'S3cureP@ss!' mariadb-local 3306

Notes:
- The password is passed on the command line (visible in shell history). Consider
  editing the script to read it securely if that matters to you.
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage; exit 0
fi

ROOT_PASSWORD="${1:-}"
CONTAINER_NAME="${2:-mariadb-local}"
HOST_PORT="${3:-3306}"
VOLUME_NAME="${CONTAINER_NAME}_data"

if [[ -z "${ROOT_PASSWORD}" ]]; then
    echo "ERROR: ROOT_PASSWORD is required."
    usage
    exit 1
fi

# Check Docker availability.
if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: Docker is not installed. Install Docker Desktop for Mac first." >&2
    exit 1
fi
if ! docker info >/dev/null 2>&1; then
    echo "ERROR: Docker is not running. Please start Docker Desktop and try again." >&2
    exit 1
fi

# If the container already exists, start it (or report it's running).
if docker ps -a --format '{{.Names}}' | grep -qx "${CONTAINER_NAME}"; then
    if docker ps --format '{{.Names}}' | grep -qx "${CONTAINER_NAME}"; then
        echo "MariaDB container '${CONTAINER_NAME}' is already running."
        echo "Connect: mysql -h 127.0.0.1 -P ${HOST_PORT} -u root -p"
        exit 0
    else
        echo "Starting existing container '${CONTAINER_NAME}'..."
        docker start "${CONTAINER_NAME}" >/dev/null
        echo "Started. Connect: mysql -h 127.0.0.1 -P ${HOST_PORT} -u root -p"
        exit 0
    fi
fi

echo "Pulling latest MariaDB image..."
docker pull mariadb:latest >/dev/null

echo "Creating and starting MariaDB container '${CONTAINER_NAME}'..."
docker run -d \
--name "${CONTAINER_NAME}" \
-p "127.0.0.1:${HOST_PORT}:3306" \
-e "MARIADB_ROOT_PASSWORD=${ROOT_PASSWORD}" \
-e "MARIADB_ROOT_HOST=%" \
-v "${VOLUME_NAME}:/var/lib/mysql" \
mariadb:latest >/dev/null

echo "Done."
echo
echo "Container name: ${CONTAINER_NAME}"
echo "Data volume:    ${VOLUME_NAME}"
echo "Listening on:   127.0.0.1:${HOST_PORT}"
echo
echo "Connect with:"
echo "  mysql -h 127.0.0.1 -P ${HOST_PORT} -u root -p"
echo
echo "Useful commands:"
echo "  docker logs -f ${CONTAINER_NAME}"
echo "  docker stop ${CONTAINER_NAME}"
echo "  docker rm -f ${CONTAINER_NAME}    # stops & removes"
echo "  docker volume rm ${VOLUME_NAME}   # deletes data (irreversible!)"
