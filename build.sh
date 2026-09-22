#!/usr/bin/env bash
#
# One script for both apps. Run it from anywhere:
#
#   ./build.sh setup            create .env files from the examples, make the
#                               shared secrets, and match them up
#   ./build.sh build [app]      build the images
#   ./build.sh up [app]         build and start, in the background
#   ./build.sh down [app]       stop and remove the containers
#   ./build.sh restart [app]    down, then up
#   ./build.sh logs [app]       follow the logs
#   ./build.sh status           what is running, and whether it is healthy
#   ./build.sh check            validate .env and the compose files, change nothing
#   ./build.sh probe            ask the intercom which endpoints it really has
#
# [app] is intercom, gatekeys, or left out for both. The two apps are
# independent: running only intercom gives you the camera view and the gate
# button, without QR codes.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPS=(intercom gatekeys)

bold=$'\e[1m'; red=$'\e[31m'; green=$'\e[32m'; yellow=$'\e[33m'; dim=$'\e[2m'; off=$'\e[0m'
[ -t 1 ] || { bold=""; red=""; green=""; yellow=""; dim=""; off=""; }

say()  { printf '%s==>%s %s\n' "$bold" "$off" "$*"; }
ok()   { printf '  %s✓%s %s\n' "$green" "$off" "$*"; }
warn() { printf '  %s!%s %s\n' "$yellow" "$off" "$*"; }
die()  { printf '%serror:%s %s\n' "$red" "$off" "$*" >&2; exit 1; }

# --------------------------------------------------------------------------- #
# Prerequisites
# --------------------------------------------------------------------------- #

compose() {
    local app="$1"; shift
    ( cd "$ROOT/$app" && docker compose "$@" )
}

require_docker() {
    command -v docker >/dev/null 2>&1 || die "docker is not installed — see docs/INSTALL.md"
    docker compose version >/dev/null 2>&1 ||
        die "the docker compose plugin is missing (docker-compose v1 will not do)"
    docker info >/dev/null 2>&1 ||
        die "cannot talk to the docker daemon — is it running, and are you in the docker group?"
}

# Which apps a command applies to: the argument, or all of them.
targets() {
    local want="${1:-}"
    if [ -z "$want" ]; then printf '%s\n' "${APPS[@]}"; return; fi
    for app in "${APPS[@]}"; do
        [ "$app" = "$want" ] && { echo "$app"; return; }
    done
    die "no such app: $want (expected one of: ${APPS[*]})"
}

# A long random string, for the cookie key and the shared token.
random_secret() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -base64 32
    else
        head -c 32 /dev/urandom | base64
    fi
}

# Set KEY=value in an .env file, replacing the line if the key is already
# there. Values are written literally, so no quoting surprises later.
set_env() {
    local file="$1" key="$2" value="$3"
    if grep -qE "^${key}=" "$file"; then
        python3 - "$file" "$key" "$value" <<'PY'
import sys
path, key, value = sys.argv[1:4]
lines = open(path, encoding="utf-8").read().splitlines(keepends=True)
out = [f"{key}={value}\n" if line.startswith(f"{key}=") else line for line in lines]
open(path, "w", encoding="utf-8").writelines(out)
PY
    else
        printf '%s=%s\n' "$key" "$value" >> "$file"
    fi
}

env_value() {
    local file="$1" key="$2"
    [ -r "$file" ] || return 0
    sed -n "s/^${key}=//p" "$file" | tail -n 1
}

# --------------------------------------------------------------------------- #
# setup — .env files and the secret both apps have to agree on
# --------------------------------------------------------------------------- #

cmd_setup() {
    for app in $(targets "${1:-}"); do
        local env_file="$ROOT/$app/.env"
        if [ -e "$env_file" ]; then
            warn "$app/.env exists, leaving it alone"
        else
            cp "$ROOT/$app/.env.example" "$env_file"
            chmod 600 "$env_file"
            ok "created $app/.env (0600) from the example"
        fi
    done

    # gatekeys needs two random secrets; generate any that are still empty.
    local gk="$ROOT/gatekeys/.env"
    if [ -f "$gk" ]; then
        for key in GATEKEYS_SECRET_KEY GATEKEYS_VERIFY_TOKEN; do
            if [ -z "$(env_value "$gk" "$key")" ]; then
                set_env "$gk" "$key" "$(random_secret)"
                ok "generated gatekeys $key"
            fi
        done
    fi

    # The intercom app presents that same token when it asks gatekeys about a
    # code, so copy it across rather than leaving two places to keep in step.
    local ic="$ROOT/intercom/.env"
    if [ -f "$gk" ] && [ -f "$ic" ]; then
        local token; token="$(env_value "$gk" GATEKEYS_VERIFY_TOKEN)"
        if [ -n "$token" ] && [ "$(env_value "$ic" GATEKEYS_TOKEN)" != "$token" ]; then
            set_env "$ic" GATEKEYS_TOKEN "$token"
            ok "copied the verify token into intercom/.env as GATEKEYS_TOKEN"
        fi
    fi

    cat <<EOF

Now edit the files you were given:

  ${bold}intercom/.env${off}   INTERCOM_HOST, INTERCOM_USER, INTERCOM_PASSWORD
                  (a dedicated ISAPI account, not the admin one)
  ${bold}gatekeys/.env${off}   GATEKEYS_USER, GATEKEYS_PASSWORD
                  ${dim}if you want QR codes; skip this app otherwise${off}

Then: ${bold}./build.sh check${off} and ${bold}./build.sh up${off}
Details in docs/INSTALL.md.
EOF
}

# --------------------------------------------------------------------------- #
# check — say what is wrong before docker does, in plainer words
# --------------------------------------------------------------------------- #

# Keys that must have a value, per app.
required_intercom=(INTERCOM_HOST INTERCOM_USER INTERCOM_PASSWORD)
required_gatekeys=(GATEKEYS_USER GATEKEYS_PASSWORD GATEKEYS_SECRET_KEY GATEKEYS_VERIFY_TOKEN)

cmd_check() {
    local problems=0
    for app in $(targets "${1:-}"); do
        say "$app"
        local env_file="$ROOT/$app/.env"
        if [ ! -f "$env_file" ]; then
            warn "no .env — run ./build.sh setup $app"
            problems=$((problems + 1))
            continue
        fi

        local perms; perms="$(stat -c '%a' "$env_file" 2>/dev/null || stat -f '%Lp' "$env_file")"
        if [ "$perms" != "600" ]; then
            warn ".env is mode $perms; it holds credentials — chmod 600 $app/.env"
        fi

        local -n required="required_$app"
        for key in "${required[@]}"; do
            if [ -z "$(env_value "$env_file" "$key")" ]; then
                warn "$key has no value in $app/.env"
                problems=$((problems + 1))
            fi
        done

        # Compose interpolates .env, so a $ in a password does not arrive as
        # typed. Better to say so than to debug a failing login later.
        for key in "${required[@]}"; do
            case "$(env_value "$env_file" "$key")" in
                *'$'*) warn "$key contains a \$, which docker compose will interpolate — pick another value";;
            esac
        done

        if compose "$app" config >/dev/null 2>&1; then
            ok "docker-compose.yml is valid"
        else
            warn "docker compose rejected the configuration:"
            # `|| true` because pipefail would otherwise take the failure we
            # are in the middle of reporting and end the script here.
            { compose "$app" config 2>&1 || true; } | tail -n 5 | sed 's/^/      /'
            problems=$((problems + 1))
        fi
    done

    # The shared secret has to match, or every code is refused with a 401 that
    # only shows up in the logs.
    local gk_token ic_token
    gk_token="$(env_value "$ROOT/gatekeys/.env" GATEKEYS_VERIFY_TOKEN)"
    ic_token="$(env_value "$ROOT/intercom/.env" GATEKEYS_TOKEN)"
    if [ -n "$gk_token" ] && [ -n "$ic_token" ] && [ "$gk_token" != "$ic_token" ]; then
        say "both"
        warn "intercom GATEKEYS_TOKEN does not match gatekeys GATEKEYS_VERIFY_TOKEN"
        problems=$((problems + 1))
    fi

    echo
    if [ "$problems" -eq 0 ]; then
        ok "nothing to fix"
    else
        die "$problems thing(s) to fix above"
    fi
}

# --------------------------------------------------------------------------- #
# The docker commands
# --------------------------------------------------------------------------- #

cmd_build() {
    for app in $(targets "${1:-}"); do
        say "building $app"
        compose "$app" build
    done
}

cmd_up() {
    for app in $(targets "${1:-}"); do
        [ -f "$ROOT/$app/.env" ] || die "$app has no .env — run ./build.sh setup"
        say "starting $app"
        compose "$app" up -d --build
    done
    echo
    cmd_status
    echo
    printf '%sFollow the logs with:%s ./build.sh logs\n' "$dim" "$off"
}

cmd_down() {
    for app in $(targets "${1:-}"); do
        say "stopping $app"
        compose "$app" down
    done
}

cmd_restart() {
    cmd_down "${1:-}"
    cmd_up "${1:-}"
}

cmd_logs() {
    local app="${1:-}"
    if [ -n "$app" ]; then
        compose "$(targets "$app")" logs -f --tail 100
        return
    fi

    # Both at once — one `docker compose` each rather than one merged
    # project, so a code that fails to open the gate reads as one story
    # without the two compose files being stitched together.
    local pids=()
    for a in "${APPS[@]}"; do
        [ -f "$ROOT/$a/.env" ] || continue
        ( cd "$ROOT/$a" && docker compose logs -f --tail 50 ) &
        pids+=($!)
    done
    [ "${#pids[@]}" -gt 0 ] || die "neither app is set up yet — run ./build.sh setup"
    trap 'kill "${pids[@]}" 2>/dev/null || true' INT TERM
    wait
}

cmd_status() {
    for app in $(targets "${1:-}"); do
        say "$app"
        compose "$app" ps --format 'table {{.Name}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null ||
            warn "not running"
    done
}

cmd_probe() {
    [ -f "$ROOT/intercom/.env" ] || die "intercom has no .env — run ./build.sh setup"
    say "asking the intercom what it supports (this never opens the gate)"
    compose intercom run --rm intercom-app python tools/probe.py "$@"
}

# --------------------------------------------------------------------------- #

usage() {
    # The header comment above is the help text; print it up to the first
    # line that is not a comment.
    awk 'NR > 1 { if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"
}

main() {
    local command="${1:-help}"
    shift || true
    case "$command" in
        setup)             require_docker; cmd_setup "$@";;
        check)             require_docker; cmd_check "$@";;
        build)             require_docker; cmd_build "$@";;
        up|start)          require_docker; cmd_up "$@";;
        down|stop)         require_docker; cmd_down "$@";;
        restart)           require_docker; cmd_restart "$@";;
        logs)              require_docker; cmd_logs "$@";;
        status|ps)         require_docker; cmd_status "$@";;
        probe)             require_docker; cmd_probe "$@";;
        help|-h|--help)    usage;;
        *)                 usage; die "unknown command: $command";;
    esac
}

main "$@"
