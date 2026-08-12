#!/usr/bin/env bash
# Create the local Keycloak realm, API client, roles and a demo user.
#
# Keycloak starts in dev mode with no realm, so this runs once after
# `docker compose up -d keycloak`. Idempotent: re-running reports what already
# exists instead of failing.
set -euo pipefail

KEYCLOAK_URL="${KEYCLOAK_URL:-http://localhost:8080}"
ADMIN_USER="${KEYCLOAK_ADMIN:-admin}"
ADMIN_PASSWORD="${KEYCLOAK_ADMIN_PASSWORD:-admin}"
REALM="${CWB_KEYCLOAK_REALM:-claims-workbench}"
CLIENT_ID="${CWB_KEYCLOAK_CLIENT_ID:-claims-workbench-api}"
FRONTEND_CLIENT_ID="${CWB_KEYCLOAK_FRONTEND_CLIENT_ID:-claims-workbench-web}"
FRONTEND_ORIGIN="${FRONTEND_ORIGIN:-http://localhost:5173}"
DEMO_USER="${DEMO_USER:-demo}"
DEMO_PASSWORD="${DEMO_PASSWORD:-demo}"

log() { printf '  %s\n' "$*"; }

echo "Waiting for Keycloak at ${KEYCLOAK_URL} ..."
for _ in $(seq 1 60); do
    if curl -fsS "${KEYCLOAK_URL}/realms/master" >/dev/null 2>&1; then
        break
    fi
    sleep 2
done

TOKEN=$(curl -fsS -X POST \
    "${KEYCLOAK_URL}/realms/master/protocol/openid-connect/token" \
    -d "client_id=admin-cli" \
    -d "username=${ADMIN_USER}" \
    -d "password=${ADMIN_PASSWORD}" \
    -d "grant_type=password" | sed -n 's/.*"access_token":"\([^"]*\)".*/\1/p')

if [ -z "${TOKEN}" ]; then
    echo "Could not obtain an admin token. Is Keycloak running with the expected credentials?" >&2
    exit 1
fi

api() {
    local method="$1" path="$2" body="${3:-}"
    if [ -n "${body}" ]; then
        curl -sS -o /dev/null -w '%{http_code}' -X "${method}" \
            "${KEYCLOAK_URL}/admin/realms${path}" \
            -H "Authorization: Bearer ${TOKEN}" \
            -H "Content-Type: application/json" \
            -d "${body}"
    else
        curl -sS -X "${method}" \
            "${KEYCLOAK_URL}/admin/realms${path}" \
            -H "Authorization: Bearer ${TOKEN}"
    fi
}

echo "Creating realm '${REALM}' ..."
status=$(api POST "" "{\"realm\":\"${REALM}\",\"enabled\":true,\"registrationAllowed\":false}")
case "${status}" in
    201) log "realm created" ;;
    409) log "realm already exists" ;;
    *) echo "unexpected status ${status} creating realm" >&2; exit 1 ;;
esac

echo "Creating backend client '${CLIENT_ID}' ..."
# A bearer-only style confidential client: the API verifies tokens, it does not
# start login flows.
status=$(api POST "/${REALM}/clients" "$(cat <<JSON
{
  "clientId": "${CLIENT_ID}",
  "enabled": true,
  "publicClient": false,
  "serviceAccountsEnabled": true,
  "standardFlowEnabled": false,
  "directAccessGrantsEnabled": true,
  "protocol": "openid-connect"
}
JSON
)")
case "${status}" in
    201) log "client created" ;;
    409) log "client already exists" ;;
    *) log "unexpected status ${status} creating backend client" ;;
esac

echo "Creating frontend client '${FRONTEND_CLIENT_ID}' ..."
# Public client using authorization code + PKCE, which is what the SPA needs.
status=$(api POST "/${REALM}/clients" "$(cat <<JSON
{
  "clientId": "${FRONTEND_CLIENT_ID}",
  "enabled": true,
  "publicClient": true,
  "standardFlowEnabled": true,
  "directAccessGrantsEnabled": true,
  "redirectUris": ["${FRONTEND_ORIGIN}/*"],
  "webOrigins": ["${FRONTEND_ORIGIN}"],
  "protocol": "openid-connect",
  "attributes": { "pkce.code.challenge.method": "S256" }
}
JSON
)")
case "${status}" in
    201) log "client created" ;;
    409) log "client already exists" ;;
    *) log "unexpected status ${status} creating frontend client" ;;
esac

echo "Creating realm roles ..."
for role in claims-adjuster claims-admin claims-supervisor; do
    status=$(api POST "/${REALM}/roles" "{\"name\":\"${role}\"}")
    case "${status}" in
        201) log "role ${role} created" ;;
        409) log "role ${role} already exists" ;;
        *) log "unexpected status ${status} creating role ${role}" ;;
    esac
done

echo "Creating demo user '${DEMO_USER}' ..."
status=$(api POST "/${REALM}/users" "$(cat <<JSON
{
  "username": "${DEMO_USER}",
  "enabled": true,
  "emailVerified": true,
  "email": "${DEMO_USER}@example.com",
  "firstName": "Demo",
  "lastName": "User",
  "credentials": [
    { "type": "password", "value": "${DEMO_PASSWORD}", "temporary": false }
  ]
}
JSON
)")
case "${status}" in
    201) log "user created" ;;
    409) log "user already exists" ;;
    *) log "unexpected status ${status} creating user" ;;
esac

USER_UUID=$(api GET "/${REALM}/users?username=${DEMO_USER}&exact=true" \
    | sed -n 's/.*"id":"\([^"]*\)".*/\1/p' | head -1)

if [ -n "${USER_UUID}" ]; then
    ROLE_JSON=$(api GET "/${REALM}/roles/claims-adjuster")
    ROLE_UUID=$(printf '%s' "${ROLE_JSON}" | sed -n 's/.*"id":"\([^"]*\)".*/\1/p' | head -1)
    api POST "/${REALM}/users/${USER_UUID}/role-mappings/realm" \
        "[{\"id\":\"${ROLE_UUID}\",\"name\":\"claims-adjuster\"}]" >/dev/null
    log "granted claims-adjuster to ${DEMO_USER}"
fi

cat <<SUMMARY

Keycloak is ready.

  Realm            ${REALM}
  Backend client   ${CLIENT_ID}
  Frontend client  ${FRONTEND_CLIENT_ID}  (public, PKCE)
  Demo user        ${DEMO_USER} / ${DEMO_PASSWORD}
  Issuer           ${KEYCLOAK_URL}/realms/${REALM}

Fetch a token to exercise the API:

  curl -s -X POST "${KEYCLOAK_URL}/realms/${REALM}/protocol/openid-connect/token" \\
    -d "client_id=${FRONTEND_CLIENT_ID}" -d "grant_type=password" \\
    -d "username=${DEMO_USER}" -d "password=${DEMO_PASSWORD}"

SUMMARY
