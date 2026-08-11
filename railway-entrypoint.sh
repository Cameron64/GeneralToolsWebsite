#!/usr/bin/env bash
# LOCAL DEPLOY SCRIPT for the Railway demo server - do NOT commit.
# Railway's startCommand (railway.json) runs this instead of the image CMD
# (/entrypoint.sh), which assumes the nginx + selenium compose stack.
set -ex
echo "railway-entrypoint: starting as $(id)"

# The demo box deliberately has NO real Zoom / Action Network / Google
# credentials: a demo publish must not create real meetings. SecretManager
# (DEBUG=False path) requires secrets.json just to import settings, so stub it.
python3 - <<'PY'
import json, os
path = "/app/tools/SecretManager/secrets.json"
if not os.path.exists(path):
    stub = {
        "ZoomAccountId": "demo-not-configured",
        "ZoomClientId": "demo-not-configured",
        "ZoomClientSecret": "demo-not-configured",
        "AnUsername": "demo@example.org",
        "AnPassword": "demo-not-configured",
        "GoogleCalId": "demo-not-configured",
        "GoogleDelegateAccount": "demo@example.org",
        "WebsiteEmailAccountUsername": "partyline-demo@example.org",
        "WebsiteEmailAccountPassword": "demo-not-configured",
    }
    with open(path, "w") as f:
        json.dump(stub, f, indent=2)
    print("Wrote stub secrets.json (demo integrations disabled)")
PY

python3 /app/manage.py collectstatic --noinput
python3 /app/manage.py migrate --noinput

# Bootstrap the admin account on first boot; harmless no-op error on reboots.
if [ -n "$DJANGO_SUPERUSER_USERNAME" ]; then
    python3 /app/manage.py createsuperuser --noinput \
        --username "$DJANGO_SUPERUSER_USERNAME" \
        --email "$DJANGO_SUPERUSER_EMAIL" || true
fi

# Demo seed (members, events, requests, link metrics, chapter tools).
#
# It REWRITES Chapter Tools rows by name on every boot, so a hand edit made in
# the admin does not survive a redeploy. railway-seed.py refuses to run unless
# DEMO_MODE is on; SEED_DEMO_DATA below only decides whether we try.
#
# The failure is still swallowed on purpose - railway.json sets
# restartPolicyType ON_FAILURE with 10 retries, so a hard exit here would
# restart-loop the service instead of giving you a box you can log into and
# inspect. But it no longer swallows QUIETLY: a half-seeded database used to
# look exactly like a clean deploy, because the healthcheck only hits the login
# page and that comes up either way.
if [ "$SEED_DEMO_DATA" = "1" ]; then
    if python3 /app/manage.py shell --command \
        "exec(open('/app/railway-seed.py', encoding='utf-8').read())"; then
        echo "SEED OK"
    else
        echo "!!! SEED FAILED (exit $?) - this database may be HALF-SEEDED."
        echo "!!! The app is still starting and the healthcheck will still pass."
        echo "!!! Some sections guard on 'does any row exist' and will not self-heal"
        echo "!!! on the next deploy. Check the traceback above before trusting the data."
    fi
fi

# No nginx here: runserver --insecure serves staticfiles even with DEBUG=False.
# Fine for a demo/dev box; the real deploy uses gunicorn behind nginx.
exec python3 /app/manage.py runserver 0.0.0.0:${PORT:-8000} --insecure --noreload
