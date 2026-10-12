#!/usr/bin/ksh
# AIX wrapper for the API synthetic monitor.
#   api-monitor.sh run    - foreground daemon (for SRC or /etc/inittab respawn)
#   api-monitor.sh once   - single cycle (for cron / Control-M / TWS / AutoSys)
# Secrets are read from a root-owned, mode 600 file and exported only to this process.

APP_HOME=${APP_HOME:-/opt/api-monitor}
CONFIG=${API_MONITOR_CONFIG:-/etc/api-monitor/config.json}
SECRETS=${API_MONITOR_SECRETS:-/etc/api-monitor/secrets.env}
PYTHON=${PYTHON:-$APP_HOME/venv/bin/python3}
MODE=${1:-run}

umask 027
if [ -r "$SECRETS" ]; then
    set -a
    . "$SECRETS"
    set +a
fi
export PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

case "$MODE" in
    run|once|status|validate|report)
        shift
        exec "$PYTHON" "$APP_HOME/monitor.py" -c "$CONFIG" "$MODE" "$@"
        ;;
    *)
        echo "usage: $0 {run|once|status|validate|report} [args]" >&2
        exit 64
        ;;
esac
