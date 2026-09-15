#!/system/bin/sh

# Local last-resort supervisor. It uses only Android's bundled shell tools and
# never touches the Wi-SUN serial port itself.

LOG=/data/local/cube_j1_watchdog.log
HEALTH=/tmp/mqtt_bridge.health
STATE=/data/local/cube_j1_watchdog.state
LAST_REBOOT=/data/local/cube_j1_watchdog.last_reboot
CONFIG=/data/local/config.json
BRIDGE_STALE=240
RADIO_STALE=1800
RESTART_WINDOW=10800
RESTART_LIMIT=3
REBOOT_COOLDOWN=21600

log() {
    now_text="$(date '+%Y-%m-%d %H:%M:%S')"
    echo "[$now_text] $*" >> "$LOG"
}

rotate_log() {
    size="$(wc -c < "$LOG" 2>/dev/null)"
    if [ -n "$size" ] && [ "$size" -gt 524288 ]; then
        mv "$LOG" "$LOG.1"
        : > "$LOG"
    fi
}

epoch() {
    date +%s
}

mtime() {
    stat -c %Y "$1" 2>/dev/null || echo 0
}

bridge_running() {
    ps 2>/dev/null | grep '[m]qtt_bridge.py' >/dev/null 2>&1
}

wifi_ip() {
    value="$(getprop dhcp.wlan0.ipaddress 2>/dev/null)"
    case "$value" in
        ''|0.0.0.0) ;;
        *) return 0 ;;
    esac
    ifconfig wlan0 2>/dev/null | grep -E 'inet addr:|inet ' | grep -v '127\.0\.0\.1' >/dev/null 2>&1
}

gateway_reachable() {
    gateway="$(getprop dhcp.wlan0.gateway 2>/dev/null)"
    [ -z "$gateway" ] && return 0
    ping -c 1 -W 2 "$gateway" >/dev/null 2>&1
}

restart_sshd() {
    if ! ps 2>/dev/null | grep '[s]shd' >/dev/null 2>&1 && \
       [ -s /data/local/ssh/sshd_config ]; then
        /usr/sbin/sshd -f /data/local/ssh/sshd_config \
            -E /data/local/ssh/sshd.log >/dev/null 2>&1
        log "ssh recovery attempted"
    fi
}

request_reboot() {
    reason="$1"
    now="$(epoch)"
    last="$(cat "$LAST_REBOOT" 2>/dev/null)"
    [ -n "$last" ] || last=0
    if [ $((now - last)) -lt "$REBOOT_COOLDOWN" ]; then
        log "reboot deferred: cooldown active; reason=$reason"
        return
    fi
    echo "$now" > "$LAST_REBOOT"
    sync
    log "reboot requested: reason=$reason"
    /system/bin/reboot
}

save_state() {
    tmp="$STATE.tmp"
    echo "$restart_count $window_at $wifi_missing_since $last_wifi_reconfigure" > "$tmp"
    mv "$tmp" "$STATE"
}

restart_count=0
window_at=0
wifi_missing_since=0
last_wifi_reconfigure=0

if [ -s "$STATE" ]; then
    read restart_count window_at wifi_missing_since last_wifi_reconfigure < "$STATE"
fi
restart_count="${restart_count:-0}"
window_at="${window_at:-0}"
wifi_missing_since="${wifi_missing_since:-0}"
last_wifi_reconfigure="${last_wifi_reconfigure:-0}"

log "local watchdog started"
sleep 180

while true; do
    now="$(epoch)"
    rotate_log
    restart_sshd
    if [ "$(getprop init.svc.adbd 2>/dev/null)" != running ]; then
        start adbd
        log "adbd recovery attempted"
    fi

    heartbeat_at="$(mtime "$HEALTH")"
    heartbeat_age=$((now - heartbeat_at))
    if ! bridge_running || [ "$heartbeat_at" -eq 0 ] || [ "$heartbeat_age" -gt "$BRIDGE_STALE" ]; then
        if [ "$window_at" -eq 0 ] || [ $((now - window_at)) -gt "$RESTART_WINDOW" ]; then
            window_at="$now"
            restart_count=0
        fi
        restart_count=$((restart_count + 1))
        save_state
        log "bridge restart requested: running=$(bridge_running && echo yes || echo no) heartbeat_age=${heartbeat_age}s count=$restart_count"
        setprop ctl.restart mqtt_ha_bridge
        if [ "$restart_count" -ge "$RESTART_LIMIT" ]; then
            save_state
            request_reboot "bridge failed ${restart_count} times in ${RESTART_WINDOW}s"
        fi
        sleep 180
        continue
    fi

    if [ "$window_at" -ne 0 ] && [ $((now - window_at)) -gt "$RESTART_WINDOW" ]; then
        restart_count=0
        window_at=0
        save_state
    fi

    bridge_state="$(sed -n 's/.*"state":"\([^"]*\)".*/\1/p' "$HEALTH" 2>/dev/null)"
    radio_at="$(sed -n 's/.*"radio_at":\([0-9][0-9]*\).*/\1/p' "$HEALTH" 2>/dev/null)"
    [ -n "$radio_at" ] || radio_at=0
    radio_anchor="$radio_at"
    if [ "$radio_anchor" -eq 0 ]; then
        radio_anchor="$(sed -n 's/.*"started_at":\([0-9][0-9]*\).*/\1/p' "$HEALTH" 2>/dev/null)"
        [ -n "$radio_anchor" ] || radio_anchor="$now"
    fi
    radio_age=$((now - radio_anchor))
    case "$bridge_state" in
      wisun_*|meter_*) monitor_radio=yes ;;
      *) monitor_radio=no ;;
    esac
    if [ "$monitor_radio" = yes ] && [ "$radio_age" -gt "$RADIO_STALE" ]; then
        if [ "$window_at" -eq 0 ] || [ $((now - window_at)) -gt "$RESTART_WINDOW" ]; then
            window_at="$now"
            restart_count=0
        fi
        restart_count=$((restart_count + 1))
        save_state
        log "bridge restart requested: Wi-SUN module has not answered for ${radio_age}s count=$restart_count"
        setprop ctl.restart mqtt_ha_bridge
        if [ "$restart_count" -ge "$RESTART_LIMIT" ]; then
            save_state
            request_reboot "Wi-SUN module remained unresponsive"
        fi
        sleep 180
        continue
    fi

    mqtt_flag="$(sed -n 's/.*"mqtt":\([01]\).*/\1/p' "$HEALTH" 2>/dev/null)"
    [ -n "$mqtt_flag" ] || mqtt_flag=0
    if wifi_ip && { [ "$mqtt_flag" -eq 1 ] || gateway_reachable; }; then
        if [ "$wifi_missing_since" -ne 0 ]; then
            wifi_missing_since=0
            save_state
        fi
    else
        if [ "$wifi_missing_since" -eq 0 ]; then
            wifi_missing_since="$now"
            log "Wi-Fi/LAN path unavailable"
            save_state
        fi
        if [ $((now - last_wifi_reconfigure)) -ge 600 ]; then
            wpa_cli -p /data/misc/wifi/sockets -i wlan0 reconfigure >/dev/null 2>&1
            last_wifi_reconfigure="$now"
            log "Wi-Fi reconfigure requested"
            save_state
        fi
        if [ $((now - wifi_missing_since)) -ge 3600 ]; then
            save_state
            request_reboot "Wi-Fi/LAN path unavailable for 3600s"
        fi
    fi

    sleep 60
done
