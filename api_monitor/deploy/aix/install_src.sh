#!/usr/bin/ksh
# Register the monitor with the AIX System Resource Controller (SRC).
# Run as root. SRC restarts the subsystem if it exits abnormally (-R),
# and stopsrc sends SIGTERM (-S -n 15 -f 9) for a graceful shutdown.
set -e
mkssys -s apimon \
       -p /opt/api-monitor/deploy/aix/api-monitor.sh \
       -a "run" \
       -u "$(id -u apimon)" \
       -S -n 15 -f 9 \
       -R \
       -i /dev/null -o /var/log/api-monitor/src.out -e /var/log/api-monitor/src.err \
       -G apimon
echo "Registered. Start:  startsrc -s apimon"
echo "Status:             lssrc -s apimon"
echo "Stop (graceful):    stopsrc -s apimon"
echo "Reload config:      kill -HUP \$(lssrc -s apimon | awk 'NR==2{print \$3}')"
echo "Auto-start at boot: mkitab 'apimon:2:once:/usr/bin/startsrc -s apimon >/dev/console 2>&1'"
