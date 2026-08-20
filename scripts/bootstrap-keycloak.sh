#!/usr/bin/env bash
# Create the local Keycloak realm, clients, roles and one user per role.
#
# Keycloak starts in dev mode with no realm, so this runs once after
# `docker compose up -d keycloak`. Idempotent: re-running reports what already
# exists instead of failing, and re-applies the client settings and the audience
# mapper so a realm created by an older version of this script is brought forward.
#
# What it produces, and why each part is needed:
#
#   claims-workbench-api    Confidential client. The API verifies tokens with it,
#                           AND the backend runs the authorization-code exchange
#                           with it — which is why the standard flow is enabled
#                           and why its secret is printed at the end.
#   audience mapper         Puts `aud: claims-workbench-api` in issued tokens.
#                           Without it Keycloak's audience is `account`, which is
#                           its catch-all and close to not validating aud at all.
#   six realm roles         Exactly the members of `app.domain.enums.Role`. The
#                           roles this script used to create were not the ones the
#                           application authorises against.
set -euo pipefail

KEYCLOAK_URL="${KEYCLOAK_URL:-http://localhost:8090}"
ADMIN_USER="${KEYCLOAK_ADMIN:-admin}"
ADMIN_PASSWORD="${KEYCLOAK_ADMIN_PASSWORD:-admin}"
REALM="${CWB_KEYCLOAK_REALM:-claims-workbench}"
CLIENT_ID="${CWB_KEYCLOAK_CLIENT_ID:-claims-workbench-api}"
FRONTEND_ORIGIN="${FRONTEND_ORIGIN:-http://localhost:5173}"
API_ORIGIN="${API_ORIGIN:-http://localhost:8000}"
# Must satisfy the realm password policy set below. Keycloak enforces that policy
# on an administratively-set password too, which is what made every user creation
# answer 400 while this was "demo".
DEMO_PASSWORD="${DEMO_PASSWORD:-Workbench2026}"

# The realm roles the application actually authorises against. Keep in step with
# `app/domain/enums.py::Role` — a role here that the enum does not know is a role
# nothing checks, and one in the enum but not here can never be granted.
ROLES=(
    fnol-officer
    claims-handler
    loss-adjuster
    claims-manager
    claims-admin
    business-admin
)

# One user per role, so every gate in the product can be exercised as somebody who
# should pass it and somebody who should not. `demo` keeps the four roles the
# fixture principal used to assert, so it lands on every screen.
declare -A USERS=(
    [demo]="fnol-officer,claims-handler,claims-manager,business-admin"
    # A real person rather than a role fixture, and every role at once: this is the
    # account the product is driven from during development, so a screen it cannot
    # reach is a screen nobody looks at. The role gates stay observable through the
    # single-role users below.
    [sitanshu]="fnol-officer,claims-handler,loss-adjuster,claims-manager,claims-admin,business-admin"
    [officer]="fnol-officer"
    [handler]="claims-handler"
    [adjuster]="loss-adjuster"
    [manager]="claims-manager"
    [admin]="claims-admin,business-admin"
)

# Addresses that are not `<username>@carrier.example`. The fixture users keep the
# example domain — nothing should ever mail them — but a real account needs the
# address its owner actually signs in with, and `/auth/login` looks the person up
# by email. Anyone absent here falls back to the derived address.
declare -A EMAILS=(
    [sitanshu]="sitanshu.boyini@aidenai.com"
)

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

# The body of the last `api` call that sent one. Keycloak explains its refusals in
# `errorMessage`, and a bare status code sent an operator to the server logs to
# find out something the response had already told us.
API_BODY_FILE="$(mktemp)"
trap 'rm -f "${API_BODY_FILE}"' EXIT

api_error() { field errorMessage < "${API_BODY_FILE}"; }

api() {
    local method="$1" path="$2" body="${3:-}"
    if [ -n "${body}" ]; then
        curl -sS -o "${API_BODY_FILE}" -w '%{http_code}' -X "${method}" \
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

# Pull the FIRST occurrence of a JSON field out of a response. Enough for ids and
# secrets; this script deliberately does not depend on `jq` being installed.
#
# `grep -o` then `head -1` rather than one `sed` substitution, and that is the whole
# point: sed's `.*` is greedy, so `.*"id":"` anchors to the LAST id on the line. On
# `GET /clients?clientId=...` — a one-line array whose client object carries nested
# protocol mappers, each with an id of its own — that returned a mapper's id rather
# than the client's. Every call keyed on it then addressed a client that did not
# exist, the client-secret lookup answered 404, and the summary printed an empty
# secret with no error anywhere.
# `|| true` because grep exits 1 on no match, and under `set -o pipefail` inside a
# `$(...)` assignment that ends the script. Absence is a legitimate answer here —
# every caller checks for an empty result — so it must not be an error.
field() { grep -o "\"$1\":\"[^\"]*\"" | head -1 | sed "s/^\"$1\":\"//;s/\"$//" || true; }

echo "Creating realm '${REALM}' ..."
status=$(api POST "" "$(cat <<JSON
{
  "realm": "${REALM}",
  "enabled": true,
  "registrationAllowed": false,
  "loginWithEmailAllowed": true,
  "resetPasswordAllowed": true,
  "bruteForceProtected": true,
  "permanentLockout": false,
  "failureFactor": 10,
  "waitIncrementSeconds": 60,
  "maxFailureWaitSeconds": 900,
  "accessTokenLifespan": 300,
  "ssoSessionIdleTimeout": 1800,
  "ssoSessionMaxLifespan": 43200,
  "passwordPolicy": "length(12) and upperCase(1) and lowerCase(1) and digits(1) and notUsername(undefined)"
}
JSON
)")
case "${status}" in
    201) log "realm created" ;;
    409) log "realm already exists" ;;
    *) echo "unexpected status ${status} creating realm" >&2; exit 1 ;;
esac

# --- The API client, which is also the BFF's client -------------------------
#
# Confidential, and now with the standard flow on: the backend redeems the
# authorization code with it. The redirect URI is the API's callback, not the
# SPA's — the browser never receives a token directly, so the SPA is not an OAuth
# client at all and does not need one.
CLIENT_BODY=$(cat <<JSON
{
  "clientId": "${CLIENT_ID}",
  "enabled": true,
  "publicClient": false,
  "serviceAccountsEnabled": true,
  "standardFlowEnabled": true,
  "directAccessGrantsEnabled": true,
  "protocol": "openid-connect",
  "redirectUris": ["${API_ORIGIN}/api/v1/auth/callback"],
  "webOrigins": ["${FRONTEND_ORIGIN}"],
  "attributes": {
    "pkce.code.challenge.method": "S256",
    "post.logout.redirect.uris": "${FRONTEND_ORIGIN}/*"
  }
}
JSON
)

echo "Creating client '${CLIENT_ID}' ..."
status=$(api POST "/${REALM}/clients" "${CLIENT_BODY}")
case "${status}" in
    201) log "client created" ;;
    409) log "client already exists" ;;
    *) log "unexpected status ${status} creating client" ;;
esac

CLIENT_UUID=$(api GET "/${REALM}/clients?clientId=${CLIENT_ID}" | field id)
if [ -z "${CLIENT_UUID}" ]; then
    echo "Could not resolve the client uuid for ${CLIENT_ID}." >&2
    exit 1
fi

# Re-applied on every run, so a realm created before the standard flow was needed
# is corrected rather than left half-configured.
api PUT "/${REALM}/clients/${CLIENT_UUID}" "${CLIENT_BODY}" >/dev/null
log "client settings applied"

echo "Adding the audience mapper ..."
status=$(api POST "/${REALM}/clients/${CLIENT_UUID}/protocol-mappers/models" "$(cat <<JSON
{
  "name": "claims-workbench-audience",
  "protocol": "openid-connect",
  "protocolMapper": "oidc-audience-mapper",
  "consentRequired": false,
  "config": {
    "included.client.audience": "${CLIENT_ID}",
    "id.token.claim": "false",
    "access.token.claim": "true"
  }
}
JSON
)")
case "${status}" in
    201) log "audience mapper created" ;;
    409) log "audience mapper already exists" ;;
    *) log "unexpected status ${status} creating audience mapper" ;;
esac

# --- Retire the old public SPA client ---------------------------------------
#
# It existed for a browser-side PKCE flow. The browser no longer talks to
# Keycloak, so leaving an enabled public client with a wildcard redirect on the
# realm is surface for nothing.
OLD_WEB_UUID=$(api GET "/${REALM}/clients?clientId=claims-workbench-web" | field id)
if [ -n "${OLD_WEB_UUID}" ]; then
    api PUT "/${REALM}/clients/${OLD_WEB_UUID}" '{"clientId":"claims-workbench-web","enabled":false}' >/dev/null
    log "disabled the legacy public client claims-workbench-web"
fi

echo "Creating realm roles ..."
for role in "${ROLES[@]}"; do
    status=$(api POST "/${REALM}/roles" "{\"name\":\"${role}\"}")
    case "${status}" in
        201) log "role ${role} created" ;;
        409) log "role ${role} already exists" ;;
        *) log "unexpected status ${status} creating role ${role}" ;;
    esac
done

#: Users the realm refused. Collected so the summary cannot describe accounts that
#: do not exist, which is exactly what it did while the password policy was
#: rejecting every one of them.
FAILED_USERS=""

echo "Creating users ..."
for username in "${!USERS[@]}"; do
    status=$(api POST "/${REALM}/users" "$(cat <<JSON
{
  "username": "${username}",
  "enabled": true,
  "emailVerified": true,
  "email": "${EMAILS[$username]:-${username}@carrier.example}",
  "firstName": "${username}",
  "lastName": "Demo",
  "credentials": [
    { "type": "password", "value": "${DEMO_PASSWORD}", "temporary": false }
  ]
}
JSON
)")
    case "${status}" in
        201) log "user ${username} created" ;;
        409) log "user ${username} already exists" ;;
        *)
            log "user ${username} REFUSED (${status}): $(api_error)"
            FAILED_USERS="${FAILED_USERS}${username} "
            ;;
    esac

    USER_UUID=$(api GET "/${REALM}/users?username=${username}&exact=true" | field id)
    if [ -z "${USER_UUID}" ]; then
        log "could not resolve ${username}; roles not granted"
        FAILED_USERS="${FAILED_USERS}${username} "
        continue
    fi

    # Built as one array so the grant is a single call: Keycloak replaces nothing
    # here, it adds, so re-running is safe.
    payload="["
    first=1
    IFS=',' read -ra wanted <<< "${USERS[$username]}"
    for role in "${wanted[@]}"; do
        role_uuid=$(api GET "/${REALM}/roles/${role}" | field id)
        [ -z "${role_uuid}" ] && continue
        [ "${first}" -eq 0 ] && payload="${payload},"
        payload="${payload}{\"id\":\"${role_uuid}\",\"name\":\"${role}\"}"
        first=0
    done
    payload="${payload}]"

    if [ "${first}" -eq 0 ]; then
        api POST "/${REALM}/users/${USER_UUID}/role-mappings/realm" "${payload}" >/dev/null
        log "granted ${USERS[$username]} to ${username}"
    fi
done

CLIENT_SECRET=$(api GET "/${REALM}/clients/${CLIENT_UUID}/client-secret" | field value)
if [ -z "${CLIENT_SECRET}" ]; then
    # Said here rather than left as a blank line in the summary. Without the secret
    # nothing about sign-in works, so this is a failure and not a footnote.
    echo "" >&2
    echo "Could not read the client secret for ${CLIENT_ID} (uuid ${CLIENT_UUID})." >&2
    echo "The client may be public, or the admin token may lack the rights to read it." >&2
    echo "Check ${KEYCLOAK_URL}/admin/master/console/#/${REALM}/clients" >&2
    exit 1
fi

cat <<SUMMARY

Keycloak is ready.

  Realm       ${REALM}
  Client      ${CLIENT_ID}  (confidential, authorization code + PKCE)
  Issuer      ${KEYCLOAK_URL}/realms/${REALM}
  Callback    ${API_ORIGIN}/api/v1/auth/callback

  Users       (password: ${DEMO_PASSWORD})
$(for u in "${!USERS[@]}"; do printf '    %-28s %s\n' "${EMAILS[$u]:-${u}@carrier.example}" "${USERS[$u]}"; done)

Put these in ClaimsWorkbench_BE/.env — sign-in cannot work without the secret:

  CWB_KEYCLOAK_SERVER_URL=${KEYCLOAK_URL}
  CWB_KEYCLOAK_REALM=${REALM}
  CWB_KEYCLOAK_CLIENT_ID=${CLIENT_ID}
  CWB_KEYCLOAK_CLIENT_SECRET=${CLIENT_SECRET}
  CWB_KEYCLOAK_AUDIENCE=${CLIENT_ID}
  CWB_AUTH_REDIRECT_URL=${API_ORIGIN}/api/v1/auth/callback
  CWB_AUTH_FRONTEND_URL=${FRONTEND_ORIGIN}

The realm password policy requires 12 characters with mixed case and a digit, and
Keycloak applies it to administratively-set passwords too — which is why the demo
password is what it is rather than something shorter.

SUMMARY

if [ -n "${FAILED_USERS}" ]; then
    # Non-zero, so this cannot read as a clean run. The realm, client and roles
    # above are usable; the named users are not. A summary that listed them anyway
    # is the failure this guards against.
    echo "Refused users: ${FAILED_USERS}" >&2
    echo "Fix the reason above and re-run; everything else is idempotent." >&2
    exit 1
fi
