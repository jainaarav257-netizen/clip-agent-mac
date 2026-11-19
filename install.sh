#!/bin/bash
# Clip Agent installer for Mac.
# Run in Terminal:
#   curl -fsSL https://raw.githubusercontent.com/jainaarav257-netizen/clip-agent-mac/main/install.sh | bash
# Installs to ~/Library/Application Support/ClipAgent, sets up Python if needed,
# and adds a Clip Agent app to ~/Applications and the Desktop.

set -u
REPO="jainaarav257-netizen/clip-agent-mac"
BRANCH="main"
RAW="https://raw.githubusercontent.com/$REPO/$BRANCH"
DIR="$HOME/Library/Application Support/ClipAgent"
APP_DIR="$HOME/Applications/Clip Agent.app"

say() { printf "\033[36m  %s\033[0m\n" "$1"; }
fail() { printf "\033[31m  %s\033[0m\n" "$1"; exit 1; }

echo ""
printf "\033[33m  Installing Clip Agent\033[0m\n"
echo ""

# Close a running copy so files can be replaced
curl -fsS -m 2 -X POST http://127.0.0.1:8765/api/quit >/dev/null 2>&1 || true
sleep 1

# 1. Python
find_python() {
    for cmd in python3.12 python3.13 python3.11 python3.10 python3.14 python3; do
        if command -v "$cmd" >/dev/null 2>&1; then
            command -v "$cmd"
            return 0
        fi
    done
    return 1
}

PY="$(find_python || true)"
if [ -z "$PY" ] && command -v brew >/dev/null 2>&1; then
    say "Installing Python 3.12 with Homebrew (one time, a couple of minutes)..."
    brew install python@3.12 >/dev/null 2>&1 || true
    PY="$(find_python || true)"
fi
if [ -z "$PY" ]; then
    fail "Python wasn't found. Install it from https://www.python.org/downloads/macos/ (or run: brew install python@3.12), then run this command again."
fi
say "Using Python: $PY"

# 2. App files + packages (reuses the app's own updater, which downloads
#    everything the manifest lists and installs requirements.txt into the venv)
mkdir -p "$DIR"
VENV_PY="$DIR/.venv/bin/python3"
if [ ! -x "$VENV_PY" ]; then
    say "Setting up (first time takes a few minutes)..."
    "$PY" -m venv "$DIR/.venv" || fail "Couldn't set up a Python virtual environment. Reinstall Python 3.12 and run this command again."
fi
[ -x "$VENV_PY" ] || fail "Couldn't set up Python for Clip Agent. Reinstall Python 3.12 from https://www.python.org/downloads/macos/, then run the install command again."

"$VENV_PY" -m pip install --upgrade pip --quiet --disable-pip-version-check --timeout 120 --retries 10

# Some python.org installs never run "Install Certificates.command", so Python's
# own HTTPS calls fail with a cert-verify error. Give this venv a working CA
# bundle via certifi so downloading the app doesn't silently fail.
"$VENV_PY" -m pip install --quiet --disable-pip-version-check certifi || true
CERT_FILE="$("$VENV_PY" -c 'import certifi; print(certifi.where())' 2>/dev/null || true)"
[ -n "$CERT_FILE" ] && export SSL_CERT_FILE="$CERT_FILE"

say "Downloading Clip Agent..."
ok=0
for try in 1 2 3; do
    # Fetched to a real file in $DIR (not piped via stdin) so updater.py's own
    # __file__-relative paths resolve inside $DIR instead of the shell's cwd.
    if curl -fsSL "$RAW/updater.py?t=$(date +%s)" -o "$DIR/updater.py" && (cd "$DIR" && "$VENV_PY" updater.py); then
        ok=1
        break
    fi
    say "That didn't finish, retrying ($try/3)..."
    sleep $((5 * try))
done
[ "$ok" = "1" ] || fail "Couldn't download Clip Agent (internet too slow, or a certificate problem). Run the install command again."

# Bring over a Gemini key from an older Clip Agent folder, if there is one
if [ ! -f "$DIR/config.json" ]; then
    for root in "$HOME/Downloads" "$HOME/Desktop" "$HOME/Documents"; do
        [ -d "$root" ] || continue
        old="$(find "$root" -maxdepth 5 -iname config.json -path '*clip-agent*' -print -quit 2>/dev/null)"
        if [ -n "$old" ]; then
            cp "$old" "$DIR/config.json"
            say "Kept your saved Gemini key."
            break
        fi
    done
fi

# 3. App bundle (so it opens like a normal Mac app, no Terminal window)
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"
cp "$DIR/icon.icns" "$APP_DIR/Contents/Resources/icon.icns" 2>/dev/null || true

cat > "$APP_DIR/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Clip Agent</string>
    <key>CFBundleDisplayName</key><string>Clip Agent</string>
    <key>CFBundleIdentifier</key><string>com.clipagent.app</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleExecutable</key><string>ClipAgent</string>
    <key>CFBundleIconFile</key><string>icon.icns</string>
    <key>LSUIElement</key><false/>
    <key>LSMinimumSystemVersion</key><string>11.0</string>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

cat > "$APP_DIR/Contents/MacOS/ClipAgent" <<SCRIPT
#!/bin/bash
cd "$DIR"
exec "$VENV_PY" "$DIR/launch.pyw" "\$@"
SCRIPT
chmod +x "$APP_DIR/Contents/MacOS/ClipAgent"

ln -sf "$APP_DIR" "$HOME/Desktop/Clip Agent.app" 2>/dev/null || true

# 4. Open it
say "Opening Clip Agent..."
open "$APP_DIR"

echo ""
printf "\033[32m  Done! Clip Agent is opening in your browser.\033[0m\n"
printf "\033[32m  Next time, double-click Clip Agent on your Desktop, or in ~/Applications.\033[0m\n"
echo ""
