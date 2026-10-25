#!/bin/bash
# Initial Let's Encrypt certificate acquisition for the Trinity production stack.
#
# Pattern: certbot certonly --webroot, with nginx serving /.well-known/acme-challenge
# from ./certbot/www. Before nginx can come up, it needs SOMETHING to load as
# ssl_certificate; we drop a self-signed stub into the same path the real cert
# will live, start nginx, then run certbot which overwrites the stub with the
# real cert. A final `nginx -s reload` picks it up.
#
# Run ONCE per (host, domain) combination, before the first
# `docker compose -f docker-compose.prod.yml up -d`.
#
# Usage:
#   DOMAIN=trinity.example.com EMAIL=admin@trinity.example.com \
#     bash scripts/init_letsencrypt.sh
#
# Set STAGING=1 to use Let's Encrypt's staging environment while you iterate
# — staging certs aren't trusted but they have far higher rate limits.

set -euo pipefail

DOMAIN="${DOMAIN:?DOMAIN must be set, e.g. DOMAIN=trinity.example.com}"
EMAIL="${EMAIL:?EMAIL must be set, e.g. EMAIL=admin@trinity.example.com}"
STAGING="${STAGING:-0}"

CERTBOT_CONF_DIR="./certbot/conf"
CERTBOT_WEBROOT_DIR="./certbot/www"
LIVE_DIR="${CERTBOT_CONF_DIR}/live/${DOMAIN}"

log() { echo "[init_letsencrypt] $*"; }

# ── 1. Replace the placeholder domain in the nginx config. ─────────────────
# nginx/conf.d/trinity.conf ships with `your-domain.com` placeholders; rewrite
# them in place the first time this script runs so nginx can start.
if grep -q "your-domain.com" nginx/conf.d/trinity.conf; then
    log "Rewriting your-domain.com → ${DOMAIN} in nginx/conf.d/trinity.conf"
    sed -i.bak "s/your-domain\.com/${DOMAIN}/g" nginx/conf.d/trinity.conf
fi

# ── 2. Lay down a stub self-signed cert so nginx can start. ────────────────
mkdir -p "${LIVE_DIR}" "${CERTBOT_WEBROOT_DIR}"

if [ ! -f "${LIVE_DIR}/fullchain.pem" ]; then
    log "Generating stub self-signed cert at ${LIVE_DIR}"
    docker run --rm \
        -v "$(pwd)/${CERTBOT_CONF_DIR}:/etc/letsencrypt" \
        --entrypoint sh \
        certbot/certbot:latest -c "\
            apk add --no-cache openssl >/dev/null 2>&1 || true && \
            mkdir -p /etc/letsencrypt/live/${DOMAIN} && \
            openssl req -x509 -nodes -newkey rsa:2048 -days 1 \
                -keyout /etc/letsencrypt/live/${DOMAIN}/privkey.pem \
                -out /etc/letsencrypt/live/${DOMAIN}/fullchain.pem \
                -subj /CN=localhost"
fi

# ── 3. Bring up nginx so the http-01 challenge can reach the webroot. ──────
log "Starting nginx so certbot can solve the http-01 challenge"
docker compose -f docker-compose.prod.yml up -d nginx

# Give nginx a moment to bind.
sleep 3

# ── 4. Delete the stub and request the real cert. ──────────────────────────
log "Removing stub cert, requesting real one"
docker run --rm \
    -v "$(pwd)/${CERTBOT_CONF_DIR}:/etc/letsencrypt" \
    --entrypoint sh \
    certbot/certbot:latest -c "rm -rf /etc/letsencrypt/live/${DOMAIN} /etc/letsencrypt/archive/${DOMAIN} /etc/letsencrypt/renewal/${DOMAIN}.conf"

STAGING_FLAG=""
if [ "${STAGING}" = "1" ]; then
    STAGING_FLAG="--staging"
    log "Using Let's Encrypt STAGING environment"
fi

docker run --rm \
    -v "$(pwd)/${CERTBOT_CONF_DIR}:/etc/letsencrypt" \
    -v "$(pwd)/${CERTBOT_WEBROOT_DIR}:/var/www/certbot" \
    certbot/certbot:latest certonly \
        --webroot \
        --webroot-path /var/www/certbot \
        --email "${EMAIL}" \
        --agree-tos \
        --no-eff-email \
        ${STAGING_FLAG} \
        -d "${DOMAIN}"

# ── 5. Reload nginx so it picks up the real cert. ──────────────────────────
log "Reloading nginx"
docker compose -f docker-compose.prod.yml exec -T nginx nginx -s reload

log "Done. Certificate now lives at ${LIVE_DIR}/fullchain.pem."
log "Renewals: cron 'docker compose -f docker-compose.prod.yml run --rm certbot renew --webroot --webroot-path /var/www/certbot && docker compose -f docker-compose.prod.yml exec nginx nginx -s reload'"
