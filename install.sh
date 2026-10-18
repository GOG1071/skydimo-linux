#!/usr/bin/env bash
#
# Installer for skydimo -- Linux driver for the Skydimo "Rainbow Music LED"
# USB LED adapter.
#
# Everything lands in your home directory; no root is needed unless you ask for
# the optional udev rule (--udev) or let the script install system packages.
#
#   ./install.sh                 install / upgrade
#   ./install.sh --check         report dependency status and exit
#   ./install.sh --udev          also install the ModemManager udev rule (sudo)
#   ./install.sh --no-deps       skip the system-package step
#   ./install.sh --uninstall     remove everything (keeps your config)
#
set -euo pipefail

APP_NAME="skydimo"
PREFIX="${XDG_DATA_HOME:-$HOME/.local/share}/$APP_NAME"
BINDIR="$HOME/.local/bin"
UNITDIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
DESKTOPDIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
CONFDIR="${XDG_CONFIG_HOME:-$HOME/.config}/$APP_NAME"
VENV="$PREFIX/venv"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DO_DEPS=1
DO_UDEV=0
CHECK_ONLY=0
UNINSTALL=0
ASSUME_YES=0

# ---------------------------------------------------------------- output ----

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    B=$'\033[1m'; R=$'\033[0m'; GRN=$'\033[32m'; YEL=$'\033[33m'; RED=$'\033[31m'
else
    B=''; R=''; GRN=''; YEL=''; RED=''
fi

say()  { printf '%s\n' "$*"; }
step() { printf '\n%s==>%s %s%s%s\n' "$GRN" "$R" "$B" "$*" "$R"; }
ok()   { printf '  %s+%s %s\n' "$GRN" "$R" "$*"; }
warn() { printf '  %s!%s %s\n' "$YEL" "$R" "$*"; }
die()  { printf '\n%serror:%s %s\n' "$RED" "$R" "$*" >&2; exit 1; }

ask_yes() {
    # ask_yes "prompt"  -> 0 for yes
    [ "$ASSUME_YES" = 1 ] && return 0
    [ -t 0 ] || return 1
    local a
    read -r -p "  $1 [y/N] " a || return 1
    case "$a" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

usage() {
    sed -n '3,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 0
}

# ------------------------------------------------------------------ args ----

for arg in "$@"; do
    case "$arg" in
        --check)     CHECK_ONLY=1 ;;
        --udev)      DO_UDEV=1 ;;
        --no-deps)   DO_DEPS=0 ;;
        --uninstall) UNINSTALL=1 ;;
        -y|--yes)    ASSUME_YES=1 ;;
        -h|--help)   usage ;;
        *)           die "unknown option: $arg (try --help)" ;;
    esac
done

# ------------------------------------------------------------ uninstall ----

do_uninstall() {
    step "Removing $APP_NAME"
    if command -v systemctl >/dev/null 2>&1; then
        systemctl --user disable --now "$APP_NAME.service" >/dev/null 2>&1 || true
    fi
    rm -f "$UNITDIR/$APP_NAME.service"
    rm -f "$DESKTOPDIR/$APP_NAME.desktop"
    rm -f "$BINDIR/$APP_NAME"
    rm -rf "$PREFIX"
    command -v systemctl >/dev/null 2>&1 && systemctl --user daemon-reload || true
    command -v update-desktop-database >/dev/null 2>&1 && \
        update-desktop-database "$DESKTOPDIR" 2>/dev/null || true
    ok "removed program files, service and launcher"
    say ""
    say "Your settings were left at $CONFDIR/config.json"
    say "The udev rule, if you installed it, is at /etc/udev/rules.d/70-skydimo.rules"
    exit 0
}

[ "$UNINSTALL" = 1 ] && do_uninstall

# ----------------------------------------------------------- distro map ----

detect_pm() {
    if   command -v rpm-ostree >/dev/null 2>&1 && [ -d /run/ostree-booted ]; then echo ostree
    elif command -v dnf        >/dev/null 2>&1; then echo dnf
    elif command -v apt-get    >/dev/null 2>&1; then echo apt
    elif command -v pacman     >/dev/null 2>&1; then echo pacman
    elif command -v zypper     >/dev/null 2>&1; then echo zypper
    else echo unknown
    fi
}

# System packages providing: PyGObject, GTK4+libadwaita typelibs, GStreamer with
# the app sink and the PipeWire source, and parec for audio capture.
packages_for() {
    case "$1" in
        dnf|ostree)
            echo "python3 python3-pip python3-gobject gtk4 libadwaita \
gstreamer1 gstreamer1-plugins-base pipewire-gstreamer pulseaudio-utils" ;;
        apt)
            echo "python3 python3-venv python3-pip python3-gi \
gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-gst-plugins-base-1.0 \
gstreamer1.0-plugins-base gstreamer1.0-pipewire pulseaudio-utils" ;;
        pacman)
            echo "python python-gobject gtk4 libadwaita \
gstreamer gst-plugins-base gst-plugin-pipewire libpulse" ;;
        zypper)
            echo "python3-gobject typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1 \
typelib-1_0-GstApp-1_0 gstreamer-plugins-base gstreamer-plugin-pipewire \
pulseaudio-utils" ;;
        *)  echo "" ;;
    esac
}

install_cmd_for() {
    case "$1" in
        dnf)    echo "sudo dnf install -y" ;;
        apt)    echo "sudo apt-get install -y" ;;
        pacman) echo "sudo pacman -S --needed --noconfirm" ;;
        zypper) echo "sudo zypper install -y" ;;
        ostree) echo "sudo rpm-ostree install" ;;
        *)      echo "" ;;
    esac
}

# ------------------------------------------------------- capability check ---

PY=""
find_python() {
    for c in python3.13 python3.12 python3.11 python3.10 python3; do
        if command -v "$c" >/dev/null 2>&1 && \
           "$c" -c 'import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)' 2>/dev/null
        then PY="$(command -v "$c")"; return 0; fi
    done
    return 1
}

MISSING=()
check_deps() {
    local quiet="${1:-0}"
    MISSING=()

    if find_python; then
        [ "$quiet" = 1 ] || ok "python: $PY ($("$PY" -c 'import platform;print(platform.python_version())'))"
    else
        MISSING+=("python>=3.10"); warn "python 3.10 or newer not found"
    fi

    if [ -n "$PY" ] && "$PY" -c 'import venv' 2>/dev/null; then
        [ "$quiet" = 1 ] || ok "python venv module"
    else
        MISSING+=("python venv"); warn "python venv module missing (Debian: python3-venv)"
    fi

    # PyGObject and the typelibs have to come from the system: pip cannot
    # reliably build them, and the venv borrows them via system-site-packages.
    if [ -n "$PY" ] && "$PY" - <<'EOF' 2>/dev/null
import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")   # appsink pull API
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gst, GstApp, Gtk, Adw  # noqa
EOF
    then
        [ "$quiet" = 1 ] || ok "PyGObject with Gst, GstApp, Gtk4, libadwaita"
    else
        MISSING+=("PyGObject + Gst/GstApp/Gtk4/Adw typelibs")
        warn "PyGObject or one of the Gst/GstApp/Gtk4/Adw typelibs is missing"
    fi

    if command -v gst-inspect-1.0 >/dev/null 2>&1 && \
       gst-inspect-1.0 pipewiresrc >/dev/null 2>&1; then
        [ "$quiet" = 1 ] || ok "GStreamer pipewiresrc (screen capture)"
    else
        MISSING+=("gstreamer pipewiresrc")
        warn "GStreamer 'pipewiresrc' not found -- ambilight needs it"
    fi

    if command -v parec >/dev/null 2>&1; then
        [ "$quiet" = 1 ] || ok "parec (audio capture)"
    else
        MISSING+=("parec")
        warn "'parec' not found -- music mode needs it"
    fi

    if command -v systemctl >/dev/null 2>&1; then
        [ "$quiet" = 1 ] || ok "systemd (for the background service)"
    else
        warn "systemctl not found -- the service will be skipped"
    fi
}

report_missing() {
    local pm="$1"
    say ""
    say "${B}Missing:${R} ${MISSING[*]}"
    local pkgs cmd
    pkgs="$(packages_for "$pm")"; cmd="$(install_cmd_for "$pm")"
    if [ -n "$pkgs" ] && [ -n "$cmd" ]; then
        say ""
        say "Install them with:"
        say "  $cmd $(echo "$pkgs" | tr -s ' \\\n' ' ')"
        if [ "$pm" = ostree ]; then
            say ""
            say "This is an image-based system, so that needs a reboot to apply."
        fi
    else
        say ""
        say "Could not recognise your package manager; install the equivalents of:"
        say "  PyGObject, GTK4 + libadwaita typelibs, GStreamer base plugins,"
        say "  the GStreamer PipeWire plugin, and pulseaudio-utils (parec)."
    fi
}

# ------------------------------------------------------------------ deps ----

PM="$(detect_pm)"

step "Checking dependencies"
check_deps
say "  package manager: $PM"

if [ "$CHECK_ONLY" = 1 ]; then
    if [ ${#MISSING[@]} -eq 0 ]; then
        say ""; ok "everything needed is present"
        exit 0
    fi
    report_missing "$PM"
    exit 1
fi

if [ ${#MISSING[@]} -gt 0 ] && [ "$DO_DEPS" = 1 ]; then
    report_missing "$PM"
    pkgs="$(packages_for "$PM")"; cmd="$(install_cmd_for "$PM")"
    if [ -n "$pkgs" ] && [ -n "$cmd" ] && [ "$PM" != ostree ]; then
        say ""
        if ask_yes "Run that now?"; then
            step "Installing system packages"
            # shellcheck disable=SC2086
            if [ "$PM" = apt ]; then sudo apt-get update || true; fi
            # shellcheck disable=SC2086
            $cmd $pkgs || die "package installation failed; install them manually and re-run"
            step "Re-checking"
            check_deps
        fi
    fi
fi

if [ ${#MISSING[@]} -gt 0 ]; then
    # Python itself is non-negotiable; the rest degrade to "that mode won't run".
    case " ${MISSING[*]} " in
        *"python>=3.10"*|*"python venv"*)
            report_missing "$PM"
            die "cannot continue without Python 3.10+ and its venv module" ;;
    esac
    say ""
    warn "continuing with missing pieces -- some features will not work:"
    for m in "${MISSING[@]}"; do warn "  $m"; done
    if ! ask_yes "Install anyway?"; then exit 1; fi
fi

# --------------------------------------------------------------- install ----

step "Installing to $PREFIX"
mkdir -p "$PREFIX" "$BINDIR" "$UNITDIR" "$DESKTOPDIR" "$CONFDIR"

# --system-site-packages is required: PyGObject and the GStreamer bindings are
# system-provided and cannot come from pip.
if [ ! -x "$VENV/bin/python" ]; then
    "$PY" -m venv --system-site-packages "$VENV"
    ok "created virtualenv"
else
    ok "reusing existing virtualenv"
fi

# Hide the per-user site dir from pip. Without this, a stray `pip install
# --user numpy` on the host makes pip consider the dependency satisfied and skip
# installing it into the venv, leaving the install quietly dependent on it.
export PYTHONNOUSERSITE=1

"$VENV/bin/python" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || \
    warn "could not upgrade pip inside the venv (continuing)"

step "Installing the skydimo package"
"$VENV/bin/python" -m pip install --quiet --upgrade "$SRC_DIR" \
    || die "pip install failed"
ok "package + Python dependencies installed"

# Launcher. A wrapper rather than a symlink so the venv's interpreter is used
# no matter how it is invoked.
cat > "$BINDIR/$APP_NAME" <<EOF
#!/usr/bin/env bash
# PYTHONNOUSERSITE keeps ~/.local/lib out of the picture, so the venv's own
# dependencies are always the ones used. System site-packages (PyGObject) still
# come through, via include-system-site-packages in pyvenv.cfg.
export PYTHONNOUSERSITE=1
exec "$VENV/bin/$APP_NAME" "\$@"
EOF
chmod +x "$BINDIR/$APP_NAME"
ok "launcher: $BINDIR/$APP_NAME"

# Rendered from templates so the absolute paths are right on any machine.
render() {
    sed -e "s|@BINDIR@|$BINDIR|g" \
        -e "s|@PREFIX@|$PREFIX|g" \
        -e "s|@APP@|$APP_NAME|g" "$1" > "$2"
}

if command -v systemctl >/dev/null 2>&1; then
    render "$SRC_DIR/packaging/skydimo.service.in" "$UNITDIR/$APP_NAME.service"
    systemctl --user daemon-reload || true
    ok "service: $UNITDIR/$APP_NAME.service"
fi

render "$SRC_DIR/packaging/skydimo.desktop.in" "$DESKTOPDIR/$APP_NAME.desktop"
command -v update-desktop-database >/dev/null 2>&1 && \
    update-desktop-database "$DESKTOPDIR" 2>/dev/null || true
ok "desktop entry: $DESKTOPDIR/$APP_NAME.desktop"

cp "$SRC_DIR/packaging/70-skydimo.rules" "$PREFIX/"
rm -f "$PREFIX/99-skydimo.rules"
cp "$SRC_DIR/README.md" "$PREFIX/" 2>/dev/null || true

if [ "$DO_UDEV" = 1 ]; then
    step "Installing the udev rule"
    # Older versions installed it as 99-, which sorts after 73-seat-late.rules
    # and so never got the uaccess ACL.
    if sudo install -m 0644 "$SRC_DIR/packaging/70-skydimo.rules" \
            /etc/udev/rules.d/70-skydimo.rules; then
        sudo rm -f /etc/udev/rules.d/99-skydimo.rules
        sudo udevadm control --reload-rules && sudo udevadm trigger || true
        ok "ModemManager will leave the adapter alone"
    else
        warn "could not install the udev rule"
    fi
fi

# ---------------------------------------------------------------- verify ----

step "Verifying"
if PYTHONNOUSERSITE=1 "$VENV/bin/python" -c 'import skydimo, numpy, serial' 2>/dev/null; then
    ok "package imports cleanly"
else
    die "the installed package does not import -- see the output above"
fi

if ! PYTHONNOUSERSITE=1 "$VENV/bin/python" - >/dev/null 2>&1 <<'EOF'
import pathlib, sys, numpy, serial
venv = pathlib.Path(sys.prefix).resolve()
for m in (numpy, serial):
    if venv not in pathlib.Path(m.__file__).resolve().parents:
        raise SystemExit(1)
EOF
then
    warn "numpy/pyserial are not inside the venv; reinstalling them there"
    "$VENV/bin/python" -m pip install --quiet --upgrade --force-reinstall \
        --no-deps numpy pyserial || die "could not install numpy/pyserial"
    ok "dependencies reinstalled inside the venv"
else
    ok "dependencies live inside the venv"
fi

if "$BINDIR/$APP_NAME" --help >/dev/null 2>&1; then
    ok "'$APP_NAME' runs"
else
    die "the launcher does not run"
fi

case ":$PATH:" in
    *":$BINDIR:"*) ok "$BINDIR is on your PATH" ;;
    *) warn "$BINDIR is not on your PATH -- add this to your shell profile:"
       say  "      export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

ADAPTER=""
if "$VENV/bin/python" - >/dev/null 2>&1 <<'EOF'
from skydimo.device import find_port
find_port()
EOF
then ADAPTER=yes; fi

step "Done"
if [ -n "$ADAPTER" ]; then
    ok "adapter detected"
else
    warn "no adapter detected right now -- plug it in, then run '$APP_NAME info'"
fi

cat <<EOF

${B}Next steps${R}

  1. Tell it how many LEDs your strip has -- this is the one setting that
     must be right. It is on the box or the product page.

       $APP_NAME gui            # or launch "Skydimo" from your app menu

     ...or without a GUI:

       $APP_NAME calibrate --leds 96

  2. Start it, and have it start at login:

       systemctl --user enable --now $APP_NAME

  Check it over:   $APP_NAME info
  Prove the wiring: $APP_NAME test
  Read the docs:   $PREFIX/README.md

EOF
