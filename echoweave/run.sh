#!/usr/bin/with-contenv bashio
# Echo Bridge — startup script for HA Supervisor addon.
# 1. Auto-discovers Music Assistant if enabled
# 2. Installs the Echoweave Proxy HA integration
# 3. Writes addon URL marker for the integration to auto-configure
# 4. Exports env vars and starts the server

set -euo pipefail

bashio::log.info "Echo Bridge v2.0.0 starting up..."

# ── Read addon configuration ───────────────────────────────────────────────
export ECHO_PUBLIC_URL="$(bashio::config 'public_url')"
export ECHO_OAUTH_CLIENT_ID="$(bashio::config 'oauth_client_id')"
export ECHO_OAUTH_CLIENT_SECRET="$(bashio::config 'oauth_client_secret')"
export ECHO_ALEXA_SKILL_ID="$(bashio::config 'alexa_skill_id')"
export ECHO_ALEXA_VALIDATION="$(bashio::config 'alexa_validation')"
export ECHO_LOG_LEVEL="$(bashio::config 'log_level')"
export ECHO_BACKEND_WS_URL="$(bashio::config 'backend_ws_url')"
export ECHO_BACKEND_WS_TOKEN="$(bashio::config 'backend_ws_token')"
export ECHO_BACKEND_INSTANCE_ID="$(bashio::config 'backend_instance_id')"
export ECHO_PROXY_PLAYER_PREFIX="$(bashio::config 'proxy_player_prefix')"
export ECHO_PROXY_PLAYER_FILTER="$(bashio::config 'proxy_player_filter')"
export ECHO_WORKER_URL="$(bashio::config 'worker_url')"
export ECHO_WORKER_SECRET="$(bashio::config 'worker_secret')"

# These may be overridden by auto-discovery below
export ECHO_LOCAL_MA_URL="$(bashio::config 'local_ma_url')"
export ECHO_LOCAL_MA_TOKEN="$(bashio::config 'local_ma_token')"

# Always store DB in the persistent /data volume
export ECHO_DB_PATH="/data/echo.db"
export ECHO_PORT="8000"

AUTO_DISCOVER_MA="$(bashio::config 'auto_discover_ma')"
AUTO_SETUP_INTEGRATION="$(bashio::config 'auto_setup_integration')"

# ── Validate required config ───────────────────────────────────────────────
if bashio::config.is_empty 'oauth_client_secret'; then
  bashio::log.fatal "oauth_client_secret is not set — account linking will fail!"
  bashio::exit.nok
fi

if bashio::config.is_empty 'public_url'; then
  bashio::log.fatal "public_url is not set — stream URLs will be broken!"
  bashio::exit.nok
fi

# ── Auto-discover Music Assistant ──────────────────────────────────────────
if [ "${AUTO_DISCOVER_MA}" = "true" ] && [ -z "${ECHO_LOCAL_MA_URL}" ]; then
  bashio::log.info "Auto-discovering Music Assistant..."

  MA_SLUGS="core_music_assistant d5369777_music_assistant local_music_assistant"
  MA_FOUND=""

  for slug in ${MA_SLUGS}; do
    MA_INFO=$(curl -sf \
      -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" \
      "http://supervisor/addons/${slug}/info" 2>/dev/null) || continue

    MA_STATE=$(echo "${MA_INFO}" | python3 -c \
      "import sys,json; print(json.load(sys.stdin).get('data',{}).get('state',''))" 2>/dev/null) || continue

    if [ "${MA_STATE}" = "started" ]; then
      MA_HOSTNAME=$(echo "${MA_INFO}" | python3 -c \
        "import sys,json; print(json.load(sys.stdin).get('data',{}).get('hostname',''))" 2>/dev/null) || continue

      MA_PORT=$(echo "${MA_INFO}" | python3 -c \
        "import sys,json; d=json.load(sys.stdin).get('data',{}); ports=d.get('network',{}); print(ports.get('8095/tcp','8095') if ports else '8095')" 2>/dev/null) || MA_PORT="8095"

      if [ -n "${MA_HOSTNAME}" ]; then
        ECHO_LOCAL_MA_URL="http://${MA_HOSTNAME}:${MA_PORT}"
        export ECHO_LOCAL_MA_URL
        MA_FOUND="${slug}"
        bashio::log.info "Found Music Assistant addon: ${slug} → ${ECHO_LOCAL_MA_URL}"
        break
      fi
    fi
  done

  if [ -z "${MA_FOUND}" ]; then
    bashio::log.warning "Music Assistant addon not found or not running. Set local_ma_url manually if MA runs externally."
  fi

  # Warn if no MA token is set — MA requires a HA long-lived access token
  if [ -n "${ECHO_LOCAL_MA_URL}" ] && [ -z "${ECHO_LOCAL_MA_TOKEN}" ]; then
    bashio::log.warning "No local_ma_token set. MA API calls will be unauthenticated."
    bashio::log.warning "If MA returns 401, go to your HA profile, create a long-lived access token, and set it as local_ma_token in addon config."
  fi
fi

# ── Auto-install HA integration ────────────────────────────────────────────
HA_CONFIG="/config"
INTEGRATION_SRC="/app/custom_components/echoweave_proxy"
INTEGRATION_DST="${HA_CONFIG}/custom_components/echoweave_proxy"

if [ "${AUTO_SETUP_INTEGRATION}" = "true" ] && [ -d "${INTEGRATION_SRC}" ]; then
  NEEDS_RESTART=false

  SRC_VERSION=$(python3 -c "import json; print(json.load(open('${INTEGRATION_SRC}/manifest.json'))['version'])" 2>/dev/null) || SRC_VERSION="unknown"

  if [ ! -d "${INTEGRATION_DST}" ]; then
    bashio::log.info "Installing Echoweave Proxy integration v${SRC_VERSION} to ${INTEGRATION_DST}..."
    mkdir -p "${INTEGRATION_DST}"
    cp -r "${INTEGRATION_SRC}/"* "${INTEGRATION_DST}/"
    touch "${INTEGRATION_DST}/.addon_managed"
    NEEDS_RESTART=true
    bashio::log.info "Integration installed successfully."
  elif [ -f "${INTEGRATION_DST}/.addon_managed" ]; then
    # Always overwrite addon-managed installations so code changes deploy on restart
    DST_VERSION=$(python3 -c "import json; print(json.load(open('${INTEGRATION_DST}/manifest.json'))['version'])" 2>/dev/null) || DST_VERSION="unknown"
    if [ "${SRC_VERSION}" != "${DST_VERSION}" ]; then
      bashio::log.info "Updating integration from v${DST_VERSION} to v${SRC_VERSION}..."
      NEEDS_RESTART=true
    else
      bashio::log.info "Refreshing addon-managed integration v${SRC_VERSION} (ensuring latest files)..."
    fi
    cp -r "${INTEGRATION_SRC}/"* "${INTEGRATION_DST}/"
    touch "${INTEGRATION_DST}/.addon_managed"
    if [ "${NEEDS_RESTART}" = "true" ]; then
      bashio::log.info "Integration updated."
    fi
  else
    bashio::log.info "Integration exists but is not addon-managed — skipping update."
  fi

  # Write addon URL marker for the integration to auto-configure
  ADDON_HOSTNAME=$(curl -sf \
    -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" \
    "http://supervisor/addons/self/info" | python3 -c \
    "import sys,json; print(json.load(sys.stdin).get('data',{}).get('hostname',''))" 2>/dev/null) || ADDON_HOSTNAME=""

  if [ -z "${ADDON_HOSTNAME}" ]; then
    ADDON_HOSTNAME=$(hostname)
  fi

  ADDON_URL="http://${ADDON_HOSTNAME}:8000"
  echo "${ADDON_URL}" > "${HA_CONFIG}/.echoweave_addon_url"
  bashio::log.info "Addon URL marker written: ${ADDON_URL}"

  if [ "${NEEDS_RESTART}" = "true" ]; then
    bashio::log.warning "═══════════════════════════════════════════════════════════"
    bashio::log.warning "  The Echoweave Proxy integration was installed/updated."
    bashio::log.warning "  Please RESTART Home Assistant to activate it."
    bashio::log.warning "═══════════════════════════════════════════════════════════"
  fi
fi

# ── Log summary ────────────────────────────────────────────────────────────
bashio::log.info "──── Configuration Summary ────"
bashio::log.info "Public URL   : ${ECHO_PUBLIC_URL}"
bashio::log.info "Validation   : ${ECHO_ALEXA_VALIDATION}"
bashio::log.info "Skill ID     : ${ECHO_ALEXA_SKILL_ID}"
bashio::log.info "DB path      : ${ECHO_DB_PATH}"
bashio::log.info "Local MA URL : ${ECHO_LOCAL_MA_URL:-<disabled>}"
bashio::log.info "Backend WSS  : ${ECHO_BACKEND_WS_URL:-<disabled>}"
bashio::log.info "Proxy prefix : ${ECHO_PROXY_PLAYER_PREFIX}"
bashio::log.info "──────────────────────────────────"

# ── Start the server ───────────────────────────────────────────────────────
exec python3 -m uvicorn app.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --workers 1 \
    --log-level "$(echo "${ECHO_LOG_LEVEL}" | tr '[:upper:]' '[:lower:]')"
