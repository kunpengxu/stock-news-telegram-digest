#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
PLIST_PATH="${LAUNCH_AGENTS_DIR}/io.kxp.newsfilter.digest.plist"
LABEL="io.kxp.newsfilter.digest"
RUN_SCRIPT="${PROJECT_DIR}/scripts/run_digest.sh"

mkdir -p "${LAUNCH_AGENTS_DIR}"
chmod +x "${RUN_SCRIPT}"

cat > "${PLIST_PATH}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>

  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>${RUN_SCRIPT}</string>
  </array>

  <key>WorkingDirectory</key>
  <string>${PROJECT_DIR}</string>

  <key>StartInterval</key>
  <integer>1800</integer>

  <key>RunAtLoad</key>
  <true/>

  <key>StandardOutPath</key>
  <string>${PROJECT_DIR}/logs/launchd.out.log</string>

  <key>StandardErrorPath</key>
  <string>${PROJECT_DIR}/logs/launchd.err.log</string>
</dict>
</plist>
EOF

launchctl unload "${PLIST_PATH}" >/dev/null 2>&1 || true
launchctl load "${PLIST_PATH}"

echo "Installed launchd job: ${LABEL}"
echo "Plist: ${PLIST_PATH}"
echo "Run every 30 minutes; script only executes at Beijing 09:00/18:00 windows."
echo "Logs:"
echo "  ${PROJECT_DIR}/logs/launchd.out.log"
echo "  ${PROJECT_DIR}/logs/launchd.err.log"
