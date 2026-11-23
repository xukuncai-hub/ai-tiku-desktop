#!/bin/bash
# AI 刷题系统：双击启动（tikuAdapter 8060 + AI 服务 5000）
BASE="$(cd "$(dirname "$0")" && pwd)"

notify() {
    notify-send "AI 题库" "$1" 2>/dev/null || kdialog --title "AI 题库" --passivepopup "$1" 5 2>/dev/null
}

# 1) 上游题库 tikuAdapter（免费源），挂了不影响 AI
if [ -x "$BASE/tiku-adapter/tikuAdapter" ] && ! pgrep -f "[t]ikuAdapter" >/dev/null; then
    ( cd "$BASE/tiku-adapter" && nohup ./tikuAdapter >> /tmp/tikuadapter.log 2>&1 & )
fi

# 2) 本地 AI 服务
if curl -s -m 2 http://localhost:5000/ >/dev/null 2>&1; then
    notify "服务已经在运行（localhost:5000）"
    exit 0
fi

cd "$BASE/ai-server" || exit 1
export PATH="$HOME/.local/bin:$PATH"
nohup uv run --locked python main.py > /tmp/ocsllm.log 2>&1 &

for _ in $(seq 1 30); do
    sleep 0.5
    if curl -s -m 1 http://localhost:5000/ >/dev/null 2>&1; then
        notify "已启动：题库 8060 + AI 5000"
        exit 0
    fi
done

notify "启动失败，请查看 /tmp/ocsllm.log"
exit 1
