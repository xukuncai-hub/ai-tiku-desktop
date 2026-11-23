#!/bin/bash
# AI 刷题系统：一键停止（tikuAdapter 8060 + AI 服务 5000）
BASE="$(cd "$(dirname "$0")" && pwd)"

notify() {
    notify-send "AI 题库" "$1" 2>/dev/null || kdialog --title "AI 题库" --passivepopup "$1" 5 2>/dev/null
}

stopped=0

stop_pattern() {
    local pattern="$1"
    local pids
    pids=$(pgrep -f "$pattern" || true)
    if [ -n "$pids" ]; then
        kill $pids 2>/dev/null || true
        for _ in $(seq 1 10); do
            sleep 0.2
            if ! pgrep -f "$pattern" >/dev/null 2>&1; then
                break
            fi
        done
        if pgrep -f "$pattern" >/dev/null 2>&1; then
            pkill -9 -f "$pattern" 2>/dev/null || true
        fi
        stopped=1
    fi
}

stop_pattern "$BASE/ai-server/.venv/bin/python main\.py"
stop_pattern "uv run --locked python main\.py"
stop_pattern "[t]ikuAdapter"

if [ "$stopped" -eq 1 ]; then
    notify "已停止：题库 8060 + AI 5000"
    echo "已停止 AI 题库与 tikuAdapter 服务。"
else
    notify "服务未在运行"
    echo "未检测到正在运行的 AI 题库服务。"
fi
