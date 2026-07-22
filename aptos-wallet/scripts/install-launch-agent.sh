#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.groove.aptos-wallet"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST="$PLIST_DIR/$LABEL.plist"
EXECUTION_ENABLED="false"

if [[ "${1:-}" == "--enable-mainnet" ]]; then
  EXECUTION_ENABLED="true"
elif [[ -n "${1:-}" ]]; then
  print -u2 "用法: $0 [--enable-mainnet]"
  exit 2
fi

NODE_BIN=""
for candidate in \
  "$HOME/.local/share/fnm/node-versions/v24.12.0/installation/bin/node" \
  "$HOME/.nvm/versions/node/v24.12.0/bin/node" \
  "$(command -v node 2>/dev/null || true)"; do
  if [[ -x "$candidate" ]]; then
    NODE_BIN="$candidate"
    break
  fi
done

if [[ -z "$NODE_BIN" ]]; then
  print -u2 "找不到 Node.js。请先安装 Node 24。"
  exit 1
fi

NODE_MAJOR="$($NODE_BIN -p 'process.versions.node.split(".")[0]')"
if (( NODE_MAJOR < 22 || NODE_MAJOR >= 25 )); then
  print -u2 "当前 Node.js 版本不兼容: $($NODE_BIN -v)。请使用 Node 24。"
  exit 1
fi

if [[ ! -f "$ROOT/dist-server/server/index.js" ]]; then
  print -u2 "缺少生产构建，请先运行: pnpm build"
  exit 1
fi

mkdir -p "$PLIST_DIR"
chmod 700 "$PLIST_DIR"

print -r -- '<?xml version="1.0" encoding="UTF-8"?>' > "$PLIST"
print -r -- '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">' >> "$PLIST"
print -r -- '<plist version="1.0"><dict>' >> "$PLIST"
print -r -- '  <key>Label</key><string>com.groove.aptos-wallet</string>' >> "$PLIST"
print -r -- "  <key>ProgramArguments</key><array><string>${NODE_BIN}</string><string>dist-server/server/index.js</string></array>" >> "$PLIST"
print -r -- "  <key>WorkingDirectory</key><string>${ROOT}</string>" >> "$PLIST"
print -r -- '  <key>EnvironmentVariables</key><dict>' >> "$PLIST"
print -r -- '    <key>NODE_ENV</key><string>production</string>' >> "$PLIST"
print -r -- '    <key>APTOS_WALLET_HOST</key><string>127.0.0.1</string>' >> "$PLIST"
print -r -- '    <key>APTOS_WALLET_API_PORT</key><string>48271</string>' >> "$PLIST"
print -r -- '    <key>APTOS_WALLET_WEB_ORIGIN</key><string>http://127.0.0.1:48272</string>' >> "$PLIST"
print -r -- "    <key>APTOS_MAINNET_EXECUTION_ENABLED</key><string>${EXECUTION_ENABLED}</string>" >> "$PLIST"
print -r -- '  </dict>' >> "$PLIST"
print -r -- '  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>' >> "$PLIST"
print -r -- '  <key>ThrottleInterval</key><integer>10</integer><key>ProcessType</key><string>Background</string>' >> "$PLIST"
print -r -- '  <key>StandardOutPath</key><string>/tmp/aptos-wallet-launchd.log</string>' >> "$PLIST"
print -r -- '  <key>StandardErrorPath</key><string>/tmp/aptos-wallet-launchd.error.log</string>' >> "$PLIST"
print -r -- '</dict></plist>' >> "$PLIST"
chmod 600 "$PLIST"
plutil -lint "$PLIST" >/dev/null

DOMAIN="gui/$(id -u)"
if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  for _ in {1..10}; do
    if ! launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
fi
bootstrapped=false
for _ in 1 2 3 4 5; do
  if launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null; then
    bootstrapped=true
    break
  fi
  if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    bootstrapped=true
    break
  fi
  sleep 1
done
if [[ "$bootstrapped" != true ]]; then
  print -u2 "无法注册本机服务 $LABEL。请检查 launchctl 日志。"
  exit 1
fi
launchctl enable "$DOMAIN/$LABEL" 2>/dev/null || true

ready=false
for _ in {1..20}; do
  if curl --fail --silent --max-time 1 "http://127.0.0.1:48271/api/v1/status" >/dev/null 2>&1; then
    ready=true
    break
  fi
  sleep 1
done
if [[ "$ready" != true ]]; then
  print -u2 "本机服务启动超时。请查看: /tmp/aptos-wallet-launchd.error.log"
  exit 1
fi

print "已安装并启动 $LABEL"
print "服务地址: http://127.0.0.1:48271"
if [[ "$EXECUTION_ENABLED" == true ]]; then
  print "主网真实转账: 已开启（仍需保险库解锁和完整确认短语）"
else
  print "主网真实转账: 已关闭（安全预览模式）"
fi
print "状态查看: launchctl print $DOMAIN/$LABEL"
print "日志: /tmp/aptos-wallet-launchd.log"
