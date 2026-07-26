#!/usr/bin/env bash
# Bootstrap a fresh SigNoz deployment: create the first admin user, mint an
# admin-scoped service-account API key, and write .env.
#
# Idempotent-ish: safe to re-run, but it will create a second service account if
# one already exists. Judges re-running Foundry from scratch run this once.
#
# The API surface here is SigNoz v0.134.x. Older docs describe /api/v1/login and
# /api/v1/pats; both were replaced by /api/v2/sessions/email_password and
# /api/v1/service_accounts respectively.
set -euo pipefail

SIGNOZ_URL="${SIGNOZ_URL:-http://localhost:8080}"
EMAIL="${PREFLIGHT_ADMIN_EMAIL:-preflight@local.dev}"
PASSWORD="${PREFLIGHT_ADMIN_PASSWORD:-Preflight!2026}"
ORG="${PREFLIGHT_ORG:-preflight}"
ENV_FILE="${ENV_FILE:-.env}"

say() { printf '\033[32m==>\033[0m %s\n' "$*"; }
die() { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

say "waiting for SigNoz at ${SIGNOZ_URL}"
for _ in $(seq 1 60); do
  if curl -sf "${SIGNOZ_URL}/api/v1/health" >/dev/null 2>&1; then break; fi
  sleep 5
done
curl -sf "${SIGNOZ_URL}/api/v1/health" >/dev/null || die "SigNoz never became healthy"

# --- first user -------------------------------------------------------------
say "registering admin user ${EMAIL}"
REG=$(curl -s -X POST "${SIGNOZ_URL}/api/v1/register" \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"${ORG}\",\"orgName\":\"${ORG}\",\"email\":\"${EMAIL}\",\"password\":\"${PASSWORD}\"}")

ORG_ID=$(printf '%s' "$REG" | python3 -c 'import json,sys
try: print(json.load(sys.stdin)["data"]["orgId"])
except Exception: print("")')

if [ -z "$ORG_ID" ]; then
  say "register did not return an org (user likely already exists) -- discovering org id"
  ORG_ID=$(curl -s -X POST "${SIGNOZ_URL}/api/v2/sessions/email_password" \
    -H 'Content-Type: application/json' \
    -d "{\"email\":\"${EMAIL}\",\"password\":\"${PASSWORD}\"}" \
    | python3 -c 'import json,sys
d=json.load(sys.stdin)
# The error body lists valid orgs when orgID is omitted.
print((d.get("data") or {}).get("orgID","") if isinstance(d.get("data"),dict) else "")')
fi
[ -n "$ORG_ID" ] || die "could not determine orgID; register response was: ${REG}"
say "org id ${ORG_ID}"

# --- session ----------------------------------------------------------------
say "logging in"
JWT=$(curl -s -X POST "${SIGNOZ_URL}/api/v2/sessions/email_password" \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"${EMAIL}\",\"password\":\"${PASSWORD}\",\"orgID\":\"${ORG_ID}\"}" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["accessToken"])')
[ -n "$JWT" ] || die "login failed"

# --- service account + admin role + key -------------------------------------
say "creating service account 'preflight-ci'"
SA_ID=$(curl -s -X POST "${SIGNOZ_URL}/api/v1/service_accounts" \
  -H "Authorization: Bearer ${JWT}" -H 'Content-Type: application/json' \
  -d '{"name":"preflight-ci"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["id"])')
[ -n "$SA_ID" ] || die "service account creation failed"

ADMIN_ROLE=$(curl -s -H "Authorization: Bearer ${JWT}" "${SIGNOZ_URL}/api/v1/roles" \
  | python3 -c 'import json,sys
print(next(r["id"] for r in json.load(sys.stdin)["data"] if r["name"]=="signoz-admin"))')

say "granting signoz-admin"
curl -s -X POST "${SIGNOZ_URL}/api/v1/service_accounts/${SA_ID}/roles" \
  -H "Authorization: Bearer ${JWT}" -H 'Content-Type: application/json' \
  -d "{\"id\":\"${ADMIN_ROLE}\"}" >/dev/null

say "minting API key"
API_KEY=$(curl -s -X POST "${SIGNOZ_URL}/api/v1/service_accounts/${SA_ID}/keys" \
  -H "Authorization: Bearer ${JWT}" -H 'Content-Type: application/json' \
  -d '{"name":"ci-key","expiresInDays":365}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["key"])')
[ -n "$API_KEY" ] || die "key creation failed"

# --- verify before writing --------------------------------------------------
say "verifying the key can read the query API"
CODE=$(curl -s -o /dev/null -w '%{http_code}' -H "SIGNOZ-API-KEY: ${API_KEY}" \
  "${SIGNOZ_URL}/api/v1/dashboards")
[ "$CODE" = "200" ] || die "key verification failed (HTTP ${CODE})"

cat > "${ENV_FILE}" <<EOF
# Written by scripts/bootstrap_signoz.sh -- gitignored, do not commit.
SIGNOZ_URL=${SIGNOZ_URL}
SIGNOZ_API_KEY=${API_KEY}
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
EOF

say "wrote ${ENV_FILE}"
say "done. SigNoz UI: ${SIGNOZ_URL}  (${EMAIL} / ${PASSWORD})"
