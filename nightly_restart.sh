#!/bin/bash
# Плановый ночной перезапуск WAHA: сбрасывает накопившуюся память (у WAHA есть утечки).
# Сессия WhatsApp сохраняется на диске, QR заново сканировать не нужно.
# Запускается из cron ночью, когда очередь сообщений не работает (она шлёт только с 9 до 21).

DIR=/opt/waha
LOG=/var/log/waha_watchdog.log
SESSION=default

log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

# Тот же lock, что у watchdog.sh — чтобы он не вмешивался во время перезапуска
exec 9>/var/lock/waha_watchdog.lock
flock -w 120 9 || { log "ночной перезапуск: не дождался lock, пропускаю"; exit 1; }

KEY=$(sed -nE 's/^WAHA_API_KEY=\s*(\S+).*/\1/p' "$DIR/.env")
MEM=$(docker exec waha_service sh -c "awk '/^anon /{print \$2}' /sys/fs/cgroup/memory.stat" 2>/dev/null)
log "ночной перезапуск WAHA (память до: $(( ${MEM:-0} / 1048576 )) MiB)"

docker restart -t 30 waha_service >> "$LOG" 2>&1

# Ждём, пока сессия снова станет WORKING (до 5 минут)
for _ in $(seq 1 60); do
    sleep 5
    STATUS=$(docker exec waha_service curl -s -m 10 -H "X-Api-Key: $KEY" "http://localhost:3000/api/sessions/$SESSION" 2>/dev/null \
        | python3 -c "import sys,json;print(json.load(sys.stdin).get('status',''))" 2>/dev/null)
    [ "$STATUS" = "WORKING" ] && break
done
MEM=$(docker exec waha_service sh -c "awk '/^anon /{print \$2}' /sys/fs/cgroup/memory.stat" 2>/dev/null)
log "ночной перезапуск WAHA завершён: сессия ${STATUS:-нет ответа}, память $(( ${MEM:-0} / 1048576 )) MiB"
