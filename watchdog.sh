#!/bin/bash
# Сторож WAHA: поднимает упавшие контейнеры и перезапускает упавшую сессию WhatsApp.
# Запускается из cron раз в минуту.

DIR=/opt/waha
LOG=/var/log/waha_watchdog.log
SESSION=default
STATE_DIR=/var/lib/waha_watchdog
mkdir -p "$STATE_DIR"

exec 9>/var/lock/waha_watchdog.lock
flock -n 9 || exit 0

# Ограничиваем размер лога
[ -f "$LOG" ] && [ "$(stat -c %s "$LOG")" -gt 5242880 ] && tail -n 2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"

log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

KEY=$(sed -nE 's/^WAHA_API_KEY=\s*(\S+).*/\1/p' "$DIR/.env")

check_container() {
    local name=$1 state health
    state=$(docker inspect -f '{{.State.Status}}' "$name" 2>/dev/null)
    health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$name" 2>/dev/null)
    if [ -z "$state" ]; then
        log "$name отсутствует — docker compose up -d"
        (cd "$DIR" && docker compose up -d >> "$LOG" 2>&1)
        return 1
    fi
    if [ "$state" != "running" ]; then
        log "$name в состоянии '$state' — запускаю"
        docker start "$name" >> "$LOG" 2>&1
        return 1
    fi
    if [ "$health" = "unhealthy" ]; then
        log "$name unhealthy — перезапускаю"
        docker restart "$name" >> "$LOG" 2>&1
        return 1
    fi
    return 0
}

check_container whatsapp_gateway
check_container waha_service || exit 0

api() { docker exec waha_service curl -s -m 15 -H "X-Api-Key: $KEY" "$@"; }

STATUS=$(api "http://localhost:3000/api/sessions/$SESSION" | python3 -c "import sys,json;print(json.load(sys.stdin).get('status',''))" 2>/dev/null)
PREV=$(cat "$STATE_DIR/status" 2>/dev/null)
echo "$STATUS" > "$STATE_DIR/status"
[ "$STATUS" != "$PREV" ] && log "сессия $SESSION: ${PREV:-?} -> ${STATUS:-нет ответа}"

case "$STATUS" in
    WORKING|STARTING)
        rm -f "$STATE_DIR/fails"
        ;;
    SCAN_QR_CODE)
        # Нужен вход по QR, перезапуск не поможет
        ;;
    FAILED|STOPPED)
        # Не чаще раза в 2 минуты, чтобы не долбить WhatsApp
        FAILS=$(( $(cat "$STATE_DIR/fails" 2>/dev/null || echo 0) + 1 ))
        echo "$FAILS" > "$STATE_DIR/fails"
        if [ $((FAILS % 2)) -eq 1 ]; then
            log "сессия $SESSION в $STATUS — перезапускаю (попытка $FAILS)"
            if [ "$STATUS" = "STOPPED" ]; then
                api -X POST "http://localhost:3000/api/sessions/$SESSION/start" > /dev/null
            else
                api -X POST "http://localhost:3000/api/sessions/$SESSION/restart" > /dev/null
            fi
        fi
        ;;
    "")
        # API не отвечает, хотя контейнер running — после 3 подряд перезапускаем контейнер
        N=$(( $(cat "$STATE_DIR/noapi" 2>/dev/null || echo 0) + 1 ))
        echo "$N" > "$STATE_DIR/noapi"
        if [ "$N" -ge 3 ]; then
            log "API WAHA не отвечает $N раз подряд — перезапускаю контейнер"
            docker restart waha_service >> "$LOG" 2>&1
            echo 0 > "$STATE_DIR/noapi"
        fi
        exit 0
        ;;
esac
echo 0 > "$STATE_DIR/noapi"
