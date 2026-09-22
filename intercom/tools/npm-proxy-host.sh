#!/usr/bin/env bash
# Optional convenience: create (or update) an nginx-proxy-manager proxy host
# for one of these apps through NPM's API, with websockets, forced SSL and an
# access list that allows only your LAN. Skip this script entirely if you use
# Caddy, Traefik, plain nginx, or no reverse proxy at all.
#
# NPM's admin credentials are read from a file, so they never appear in a
# shell history or transcript:
#
#   printf 'NPM_EMAIL=admin@example.com\nNPM_PASSWORD=...\n' > ~/.npm-admin
#   chmod 600 ~/.npm-admin
#
# Then, with your own values:
#
#   DOMAIN=gate.example.lan FORWARD_HOST=192.0.2.1 FORWARD_PORT=8099 \
#   CERT_NAME=my-cert ACL_CIDR=192.0.2.0/24 NPM_URL=http://192.0.2.1:81 \
#       tools/npm-proxy-host.sh
#
# Never create a public DNS record for that hostname, and never port-forward
# to NPM: these apps have no authentication of their own. See ../SECURITY.md.
set -euo pipefail

NPM_URL="${NPM_URL:?set NPM_URL, e.g. http://192.0.2.1:81}"
CRED_FILE="${NPM_CRED_FILE:-$HOME/.npm-admin}"
DOMAIN="${DOMAIN:?set DOMAIN, e.g. gate.example.lan}"
FORWARD_HOST="${FORWARD_HOST:?set FORWARD_HOST, the host address the app is published on}"
FORWARD_PORT="${FORWARD_PORT:-8099}"
# Name of an existing certificate in NPM (a wildcard for your internal domain
# is the easy option; Let's Encrypt cannot validate a name that never resolves
# publicly).
CERT_NAME="${CERT_NAME:?set CERT_NAME, the name of a certificate already in NPM}"
ACL_NAME="${ACL_NAME:-home-lan-only}"
ACL_CIDR="${ACL_CIDR:?set ACL_CIDR, the only network allowed in, e.g. 192.0.2.0/24}"
# Optional nginx snippet for the host's "Advanced" tab. If it defines its own
# `location /`, NPM drops its default location AND the access-list rules that
# live inside it, so such a snippet must repeat its own allow/deny.
ADVANCED_CONFIG_FILE="${ADVANCED_CONFIG_FILE:-}"

[ -r "$CRED_FILE" ] || { echo "credentials file $CRED_FILE not found (see header of this script)"; exit 2; }
# shellcheck disable=SC1090
source "$CRED_FILE"
export NPM_EMAIL NPM_PASSWORD
: "${NPM_EMAIL:?NPM_EMAIL missing in $CRED_FILE}" "${NPM_PASSWORD:?NPM_PASSWORD missing in $CRED_FILE}"

json() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

TOKEN=$(curl -sf -X POST "$NPM_URL/api/tokens" -H 'Content-Type: application/json' \
  -d "$(python3 -c "import json,os; print(json.dumps({'identity': os.environ['NPM_EMAIL'], 'secret': os.environ['NPM_PASSWORD']}))")" \
  | json 'd["token"]') || { echo "login failed"; exit 1; }
auth=(-H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json')

# Certificate id for the wildcard cert.
CERT_ID=$(curl -sf "${auth[@]}" "$NPM_URL/api/nginx/certificates" | json "next((c['id'] for c in d if c.get('nice_name')=='$CERT_NAME'), '')")
[ -n "$CERT_ID" ] || { echo "certificate $CERT_NAME not found"; exit 1; }
echo "certificate: $CERT_NAME (id $CERT_ID)"

# Access list: allow the home LAN, deny everything else.
ACL_ID=$(curl -sf "${auth[@]}" "$NPM_URL/api/nginx/access-lists" | json "next((a['id'] for a in d if a['name']=='$ACL_NAME'), '')")
if [ -z "$ACL_ID" ]; then
  ACL_ID=$(curl -sf "${auth[@]}" -X POST "$NPM_URL/api/nginx/access-lists" -d "{
    \"name\": \"$ACL_NAME\", \"satisfy_any\": true, \"pass_auth\": false,
    \"items\": [], \"clients\": [{\"address\": \"$ACL_CIDR\", \"directive\": \"allow\"}]
  }" | json 'd["id"]')
  echo "access list: created $ACL_NAME (id $ACL_ID)"
else
  echo "access list: $ACL_NAME exists (id $ACL_ID)"
fi

ADV_JSON='""'
if [ -n "$ADVANCED_CONFIG_FILE" ]; then
  ADV_JSON=$(python3 -c "import json,sys; print(json.dumps(open(sys.argv[1]).read()))" "$ADVANCED_CONFIG_FILE")
  echo "advanced config: $ADVANCED_CONFIG_FILE"
fi

BODY=$(cat <<JSON
{
  "domain_names": ["$DOMAIN"],
  "forward_scheme": "http",
  "forward_host": "$FORWARD_HOST",
  "forward_port": $FORWARD_PORT,
  "certificate_id": $CERT_ID,
  "ssl_forced": true,
  "http2_support": true,
  "hsts_enabled": false,
  "hsts_subdomains": false,
  "block_exploits": true,
  "caching_enabled": false,
  "allow_websocket_upgrade": true,
  "access_list_id": $ACL_ID,
  "advanced_config": $ADV_JSON,
  "meta": {"letsencrypt_agree": false, "dns_challenge": false},
  "locations": []
}
JSON
)

EXISTING=$(curl -sf "${auth[@]}" "$NPM_URL/api/nginx/proxy-hosts" | json "next((h['id'] for h in d if '$DOMAIN' in h['domain_names']), '')")
if [ -n "$EXISTING" ]; then
  curl -sf "${auth[@]}" -X PUT "$NPM_URL/api/nginx/proxy-hosts/$EXISTING" -d "$BODY" >/dev/null
  echo "proxy host: updated $DOMAIN (id $EXISTING)"
else
  NEW=$(curl -sf "${auth[@]}" -X POST "$NPM_URL/api/nginx/proxy-hosts" -d "$BODY" | json 'd["id"]')
  echo "proxy host: created $DOMAIN (id $NEW)"
fi

echo "check: curl -sk --resolve $DOMAIN:443:$FORWARD_HOST https://$DOMAIN/api/health"
