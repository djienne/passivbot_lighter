#!/bin/bash
# Monitor bot every 30 minutes for 12 hours (24 checks)
LOG="/home/ubuntu/passivbot_lighter/bot.log"
MONITOR_LOG="/home/ubuntu/passivbot_lighter/monitor.log"
echo "$(date -u) — Bot monitor started (12h, 30min intervals)" > "$MONITOR_LOG"

for i in $(seq 1 24); do
    sleep 1800
    echo "--- Check $i/24 at $(date -u) ---" >> "$MONITOR_LOG"
    
    # Check if process is alive
    if pgrep -f "python3.*hype_top" > /dev/null; then
        echo "  Process: RUNNING (PID $(pgrep -f 'python3.*hype_top'))" >> "$MONITOR_LOG"
    else
        echo "  Process: DEAD — restarting..." >> "$MONITOR_LOG"
        cd /home/ubuntu/passivbot_lighter
        SKIP_RUST_COMPILE=1 nohup python3 -u src/main.py configs/hype_top.json >> "$LOG" 2>&1 &
        echo "  Restarted with PID $!" >> "$MONITOR_LOG"
    fi
    
    # Count 429s
    COUNT429=$(grep -c "429" "$LOG" 2>/dev/null || echo 0)
    echo "  429 errors total: $COUNT429" >> "$MONITOR_LOG"
    
    # Last 3 log lines
    echo "  Last log:" >> "$MONITOR_LOG"
    tail -3 "$LOG" | sed 's/^/    /' >> "$MONITOR_LOG"
done

echo "$(date -u) — Monitor completed" >> "$MONITOR_LOG"
