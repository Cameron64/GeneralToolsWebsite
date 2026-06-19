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

# Idempotent demo seed (members, events, requests, link metrics).
if [ "$SEED_DEMO_DATA" = "1" ]; then
    python3 /app/manage.py shell --command \
        "exec(open('/app/railway-seed.py', encoding='utf-8').read())" || true
fi

# No nginx here: runserver --insecure serves staticfiles even with DEBUG=False.
# Fine for a demo/dev box; the real deploy uses gunicorn behind nginx.
exec python3 /app/manage.py runserver 0.0.0.0:${PORT:-8000} --insecure --noreload
