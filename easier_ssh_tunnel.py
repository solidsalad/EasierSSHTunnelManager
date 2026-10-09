#!/usr/bin/env python3
"""
Easier SSH Tunnel Manager - GUI for managing SSH tunnels on Ubuntu/GNOME.
Fork of Easy SSH Tunnel Manager (ProjectMakersDE, MIT).
"""

import gi
gi.require_version('Gtk', '3.0')
try:
    gi.require_version('AppIndicator3', '0.1')
    from gi.repository import AppIndicator3
except (ValueError, ImportError):
    try:
        gi.require_version('AyatanaAppIndicator3', '0.1')
        from gi.repository import AyatanaAppIndicator3 as AppIndicator3
    except (ValueError, ImportError):
        AppIndicator3 = None  # no tray library: window mode only
from gi.repository import Gtk, Gdk, Gio, GLib, GObject, Pango
import cairo
import subprocess
import json
import math
import os
import shutil
import signal
import re
import socket
import threading
import time
import uuid
from pathlib import Path

APP_ID = "easier-ssh-tunnel"
APP_NAME = "Easier SSH Tunnel Manager"

# Status colors
COLOR_GREEN = "#73bf69"
COLOR_RED = "#f2495c"
COLOR_ORANGE = "#ff9830"
COLOR_YELLOW = "#fade2a"
COLOR_DIM = "#8e8e9a"
COLOR_GREY = "#6e7180"

# Statuses that mean the port is open; the row switch shows these as ON
ON_STATUSES = ("Running", "Connecting", "External")

# Default colors handed out to tunnels without one
TUNNEL_PALETTE = ["#7eb26d", "#eab839", "#6ed0e0", "#ef843c",
                  "#e24d42", "#1f78c1", "#ba43a9", "#705da0"]

HEX_RE = re.compile(r'^#?([0-9a-fA-F]{6})$')

# ssh options that take a value, needed to find the destination in a command line
SSH_OPTS_WITH_ARG = set("BbcDEeFIiJLlmOoPpQRSWw")


def normalize_hex(value):
    """Return '#rrggbb' for a valid hex color, else None"""
    match = HEX_RE.match((value or '').strip())
    return f"#{match.group(1).lower()}" if match else None


def _srgb_to_linear(c):
    c /= 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(c):
    c = max(0.0, min(1.0, c))
    return 255 * (12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055)


def hex_to_oklch(hex_color):
    """'#rrggbb' -> (lightness 0-1, chroma, hue in degrees) in the OKLCH color space"""
    value = normalize_hex(hex_color) or COLOR_DIM
    r, g, b = (_srgb_to_linear(int(value[i:i + 2], 16)) for i in (1, 3, 5))
    l = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    lightness = 0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s
    a = 1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s
    b2 = 0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s
    return lightness, math.hypot(a, b2), math.degrees(math.atan2(b2, a)) % 360


def oklch_to_hex(lightness, chroma, hue):
    """OKLCH -> '#rrggbb'; chroma is reduced until the color fits in sRGB"""
    for _ in range(60):
        a = chroma * math.cos(math.radians(hue))
        b = chroma * math.sin(math.radians(hue))
        l = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
        m = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
        s = (lightness - 0.0894841775 * a - 1.2914855480 * b) ** 3
        rgb = (4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
               -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
               -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s)
        if all(-1e-4 <= c <= 1 + 1e-4 for c in rgb):
            break
        chroma *= 0.95
    return "#" + "".join(f"{round(_linear_to_srgb(c)):02x}" for c in rgb)


# 5 shades: 2 tints, the middle, 2 darker shades. How far each tint is toward almost white,
# and each darker shade toward almost black (the ends themselves are not offered)
LIGHT_STEPS = [0.63, 0.32]
DARK_STEPS = [0.32, 0.61]
SHADE_COUNT = len(LIGHT_STEPS) + 1 + len(DARK_STEPS)
# Lightness gaps from the middle for muted colors, about those of a typical vivid row
MUTED_OFFSETS = [0.165, 0.085, 0.0, -0.16, -0.305]
MIDDLE_TINT = len(LIGHT_STEPS)
WHITE_LIGHTNESS, BLACK_LIGHTNESS = 0.97, 0.22

# Hue shifting: lighter shades turn slightly toward yellow, darker ones toward purple, by at
# most these many degrees, reached three steps away from the middle
LIGHT_HUE, DARK_HUE = 100.0, 300.0
LIGHT_SHIFT, DARK_SHIFT = 11.0, 7.7


def _shifted_hue(hue, delta):
    target, limit = (LIGHT_HUE, LIGHT_SHIFT) if delta > 0 else (DARK_HUE, DARK_SHIFT)
    diff = (target - hue + 180) % 360 - 180
    amount = min(1.0, abs(delta) / 0.25) * limit
    return (hue + max(-amount, min(amount, diff))) % 360


def _in_gamut(lightness, chroma, hue):
    a = chroma * math.cos(math.radians(hue))
    b = chroma * math.sin(math.radians(hue))
    l = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s = (lightness - 0.0894841775 * a - 1.2914855480 * b) ** 3
    rgb = (4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
           -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
           -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s)
    return all(-1e-4 <= c <= 1 + 1e-4 for c in rgb)


def _max_chroma(lightness, hue):
    """Highest chroma the screen can show at this lightness and hue"""
    low, high = 0.0, 0.4
    for _ in range(24):
        mid = (low + high) / 2
        low, high = (mid, high) if _in_gamut(lightness, mid, hue) else (low, mid)
    return low


def _preset_lightness(hue):
    """Lightness of the theme preset closest in hue"""
    def distance(color):
        diff = abs(hex_to_oklch(color)[2] - hue)
        return min(diff, 360 - diff)
    return hex_to_oklch(min(BASE_COLORS, key=distance))[0]


def tint_row(color):
    """5 shades of a color, from light to dark. The middle tile (index 2) is the
    most vibrant one: a vivid color is the middle itself; for a muted color the middle has the
    same hue at a typical lightness, about as vibrant as the color, and the color itself takes
    the tile matching its lightness."""
    color = normalize_hex(color)
    lightness, chroma, hue = hex_to_oklch(color)
    vivid = chroma >= 0.08 and chroma >= 0.8 * _max_chroma(lightness, hue)
    if vivid:
        mid_light, mid_chroma = lightness, chroma
    else:
        mid_light = (lightness + _preset_lightness(hue)) / 2
        gain = _max_chroma(mid_light, hue) / max(_max_chroma(lightness, hue), 1e-6)
        mid_chroma = chroma * max(1.0, min(1.25, gain))
    if vivid:
        levels = ([mid_light + (WHITE_LIGHTNESS - mid_light) * t for t in LIGHT_STEPS] + [mid_light]
                  + [mid_light - (mid_light - BLACK_LIGHTNESS) * t for t in DARK_STEPS])
    else:
        # Fixed gaps around the middle; the window moves as a whole: first to stay on screen,
        # then so a color lighter than the top (or darker than the bottom) becomes the new
        # ceiling (or floor)
        levels = [mid_light + offset for offset in MUTED_OFFSETS]
        shift = 0.0
        if levels[0] > max(lightness, WHITE_LIGHTNESS):
            shift = max(lightness, WHITE_LIGHTNESS) - levels[0]
        elif levels[-1] < min(lightness, 0.2):
            shift = min(lightness, 0.2) - levels[-1]
        levels = [level + shift for level in levels]
        if lightness > levels[0]:
            shift = lightness - levels[0]
        elif lightness < levels[-1]:
            shift = lightness - levels[-1]
        else:
            shift = 0.0
        levels = [level + shift for level in levels]
        mid_light = levels[MIDDLE_TINT]
    fades = ([1 - 0.85 * t ** 1.6 for t in LIGHT_STEPS] + [1.0]
             + [1 - 0.5 * t ** 2 for t in DARK_STEPS])
    row = [oklch_to_hex(level, mid_chroma * fade, _shifted_hue(hue, level - mid_light))
           for level, fade in zip(levels, fades)]
    slot = MIDDLE_TINT if vivid else min(range(len(levels)), key=lambda i: abs(levels[i] - lightness))
    row[slot] = color
    return row


class ColorMemory:
    """Your colors, recent colors and the shade rows they belong to, in colors.json"""

    MAX_RECENT = 12
    MAX_MINE = 11
    MAX_ROWS = 200

    def __init__(self, config_dir):
        self.path = Path(config_dir) / 'colors.json'
        try:
            with open(self.path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        self.recent = data.get('recent', [])
        self.rows = data.get('rows', [])
        # Your colors always shows the middle shade of each row, so the list is not a mix of
        # light and dark tints
        self.mine = []
        for color in data.get('mine', []):
            middle = self.row_for(color)[MIDDLE_TINT]
            if middle not in self.mine:
                self.mine.append(middle)

    def _save(self):
        try:
            with open(self.path, 'w') as f:
                json.dump({'recent': self.recent, 'mine': self.mine, 'rows': self.rows}, f, indent=2)
        except OSError as e:
            print(f"Error saving colors: {e}")

    def row_for(self, color):
        """The saved shade row that contains color, else a new one around it"""
        color = normalize_hex(color)
        for row in self.rows:
            if color in row:
                return list(row)
        return tint_row(color)

    def _keep_row(self, row):
        if row in self.rows:
            self.rows.remove(row)
        self.rows = ([row] + self.rows)[:self.MAX_ROWS]

    def add_mine(self, color):
        """A custom color for Your colors, shown as the middle shade of its row"""
        row = self.row_for(color)
        self._keep_row(row)
        middle = row[MIDDLE_TINT]
        self.mine = ([middle] + [c for c in self.mine if c != middle])[:self.MAX_MINE]
        self._save()

    def remove_mine(self, color):
        """Take a color out of Your colors; its shade row stays for tunnels that use it"""
        self.mine = [c for c in self.mine if c != color]
        self._save()

    def replace_mine(self, old, color):
        """Give an entry of Your colors a new base color, in the same place"""
        row = self.row_for(color)
        self._keep_row(row)
        middle = row[MIDDLE_TINT]
        self.mine = [middle if c == old else c for c in self.mine if c != middle or c == old]
        self._save()
        return middle

    def remember(self, color, row):
        """After a save: the exact color goes to Recent, the row's middle shade to Your colors"""
        color = normalize_hex(color)
        if not color:
            return
        if not row or color not in row:
            row = self.row_for(color)
        self._keep_row(row)
        self.recent = ([color] + [c for c in self.recent if c != color])[:self.MAX_RECENT]
        if not any(c in row for c in self.mine):
            self.mine = ([row[MIDDLE_TINT]] + self.mine)[:self.MAX_MINE]
        self._save()


# Theme presets: hues around the color wheel at 94% of full chroma, so no preset is a shade
# of another and the middle of a shade row is not overly loud. Presets closer than 0.049 in
# OKLab to another one were left out, which leaves 24 clearly different colors.
BASE_COLORS = ["#b45ef4", "#9869f4", "#657ef5", "#2f9ef6", "#2dadea", "#33c1d9",
               "#3ad5ca", "#3ce6a9", "#43ed84", "#6df745", "#b0ef3a", "#d8ea38",
               "#fcd03b", "#fab839", "#f8a335", "#fb8925", "#f96d23", "#f6573d",
               "#f93f55", "#f93275", "#f72b95", "#eb34b4", "#dd3fd0", "#cc4ae9"]


def tunnel_port(config):
    """Local port a tunnel listens on, or '' for remote tunnels"""
    if config.get('type') == 'remote':
        return ''
    forwards = config.get('forwards') or []
    if forwards:
        return str(forwards[0].get('local_port', ''))
    return str(config.get('local_port', ''))


def parse_ssh_argv(argv):
    """Split an ssh argv into forwards, port, user and host.

    Returns dict with keys D, L, R (lists of specs), port, user, host.
    """
    result = {'D': [], 'L': [], 'R': [], 'port': '', 'user': '', 'host': ''}
    positional = []
    i = 1 if argv and os.path.basename(argv[0]) == 'ssh' else 0
    while i < len(argv):
        arg = argv[i]
        if arg.startswith('-') and len(arg) > 1:
            flag = arg[1]
            if flag in SSH_OPTS_WITH_ARG:
                value = arg[2:] if len(arg) > 2 else (argv[i + 1] if i + 1 < len(argv) else '')
                i += 1 if len(arg) > 2 else 2
                if flag in 'DLR':
                    result[flag].append(value)
                elif flag == 'p':
                    result['port'] = value
                elif flag == 'l':
                    result['user'] = value
                continue
            i += 1
            continue
        positional.append(arg)
        i += 1
    if positional:
        dest = positional[0]
        if '@' in dest:
            result['user'], result['host'] = dest.rsplit('@', 1)
        else:
            result['host'] = dest
    return result


def spec_local_port(spec):
    """Local port of a -D/-L spec like '1080', 'localhost:1080' or '8080:host:80'"""
    parts = spec.split(':')
    if len(parts) in (1, 2):
        return parts[-1]
    if len(parts) >= 3:
        return parts[-3]
    return ''


def socks_probe(port):
    """True if a SOCKS5 server answers on 127.0.0.1:port"""
    try:
        with socket.create_connection(('127.0.0.1', int(port)), timeout=0.5) as sock:
            sock.settimeout(0.5)
            sock.sendall(b'\x05\x01\x00')
            return sock.recv(2) == b'\x05\x00'
    except (OSError, ValueError):
        return False


class PortScanner:
    """Lists local TCP listeners with owning process, cached for a second"""

    SS_LINE = re.compile(r'(\S+):(\d+)\s+\S+\s+users:\(\("([^"]+)",pid=(\d+)')
    MUX_RE = re.compile(r'^ssh: (\S*ssh_mux_(?:tunnel_)?(.+)_(\d+)_([^_\s]+)) \[mux\]')

    def __init__(self):
        self._cache = {}
        self._stamp = 0.0

    def listeners(self):
        """Return {port: {'pid': int, 'proc': str}}"""
        if time.monotonic() - self._stamp < 1.0:
            return self._cache
        found = {}
        try:
            out = subprocess.run(['ss', '-ltnpH'], capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.SubprocessError):
            out = ''
        for line in out.splitlines():
            match = self.SS_LINE.search(line)
            if match:
                found.setdefault(match.group(2), {'pid': int(match.group(4)), 'proc': match.group(3)})
            else:
                # Socket owned by another user: port known, process not
                cols = line.split()
                if len(cols) >= 4 and ':' in cols[3]:
                    found.setdefault(cols[3].rsplit(':', 1)[1], {'pid': 0, 'proc': '?'})
        self._cache = found
        self._stamp = time.monotonic()
        return found

    @staticmethod
    def cmdline(pid):
        try:
            with open(f'/proc/{pid}/cmdline', 'rb') as f:
                return [a.decode(errors='replace') for a in f.read().split(b'\0') if a]
        except OSError:
            return []

    def external_tunnels(self, own_pids):
        """Describe ssh-owned listening ports not started by this app"""
        tunnels = []
        for port, info in sorted(self.listeners().items(), key=lambda kv: int(kv[0])):
            if info['proc'] != 'ssh' or info['pid'] in own_pids:
                continue
            argv = self.cmdline(info['pid'])
            entry = {'port': port, 'pid': info['pid'], 'command': ' '.join(argv),
                     'type': '', 'user': '', 'host': '', 'ssh_port': '',
                     'remote_host': '', 'remote_port': '', 'control_path': ''}
            mux = self.MUX_RE.match(' '.join(argv))
            if mux:
                # Forward was added to a shared ControlMaster connection
                entry.update(control_path=mux.group(1), host=mux.group(2),
                             ssh_port=mux.group(3), user=mux.group(4))
                entry['type'] = 'dynamic' if socks_probe(port) else 'local'
            else:
                parsed = parse_ssh_argv(argv)
                entry.update(user=parsed['user'], host=parsed['host'], ssh_port=parsed['port'])
                for spec in parsed['D']:
                    if spec_local_port(spec) == port:
                        entry['type'] = 'dynamic'
                for spec in parsed['L']:
                    if spec_local_port(spec) == port:
                        parts = spec.split(':')
                        entry.update(type='local', remote_host=parts[-2], remote_port=parts[-1])
                if not entry['type']:
                    entry['type'] = 'dynamic' if socks_probe(port) else 'local'
            tunnels.append(entry)
        return tunnels


class SSHTunnelManager:
    """Manages SSH tunnel processes, keyed by the tunnel's stable id"""

    def __init__(self):
        self.tunnels = {}  # tunnel_id -> process
        self.messages = {}  # tunnel_id -> last ssh error line
        self.wanted = set()  # tunnel ids the user switched on; down while wanted = Stopped
        self.scanner = PortScanner()

    def _read_stderr(self, tunnel_id, process):
        """Keep the last stderr line of a tunnel as its message"""
        for raw in iter(process.stderr.readline, b''):
            line = raw.decode(errors='replace').strip()
            if line:
                self.messages[tunnel_id] = line
        code = process.wait()
        if self.tunnels.get(tunnel_id) is process and code not in (0, -signal.SIGTERM):
            if not self.messages.get(tunnel_id):
                self.messages[tunnel_id] = f"ssh exited with code {code}"

    def port_owner(self, port):
        """Describe who listens on a local port, or None"""
        info = self.scanner.listeners().get(str(port)) if port else None
        if not info:
            return None
        return info

    def start_tunnel(self, tunnel_id, config):
        """Start an SSH tunnel with the given configuration"""
        if tunnel_id in self.tunnels and self.tunnels[tunnel_id].poll() is None:
            return False, "Tunnel already running"
        self.wanted.add(tunnel_id)

        port = tunnel_port(config)
        owner = self.port_owner(port)
        if owner:
            who = f"{owner['proc']} (pid {owner['pid']})" if owner['pid'] else "another user"
            message = f"port {port} already in use by {who}"
            self.messages[tunnel_id] = message
            return False, message

        tunnel_type = config.get('type', 'local')
        ssh_host = config.get('ssh_host', '')
        ssh_user = config.get('ssh_user', '')
        ssh_port = str(config.get('ssh_port', '') or '').strip()

        # Own connection per tunnel: with a shared ControlMaster the forward lives in the
        # master and survives stopping this process.
        cmd = ['ssh', '-N', '-o', 'ControlMaster=no', '-o', 'ControlPath=none',
               '-o', 'ExitOnForwardFailure=yes', '-o', 'LogLevel=ERROR']

        if tunnel_type == 'local':
            # Local port forwarding: -L local_port:remote_host:remote_port
            # Support both single port forward and multiple port forwards
            forwards = config.get('forwards', [])
            if forwards:
                # Multiple port forwards
                for forward in forwards:
                    local_port = forward.get('local_port', '')
                    remote_host = forward.get('remote_host', '')
                    remote_port = forward.get('remote_port', '')
                    tunnel_spec = f"{local_port}:{remote_host}:{remote_port}"
                    cmd.extend(['-L', tunnel_spec])
            else:
                # Legacy single port forward (backward compatibility)
                local_port = config.get('local_port', '')
                remote_host = config.get('remote_host', '')
                remote_port = config.get('remote_port', '')
                tunnel_spec = f"{local_port}:{remote_host}:{remote_port}"
                cmd.extend(['-L', tunnel_spec])
        elif tunnel_type == 'remote':
            # Remote port forwarding: -R remote_port:remote_host:local_port
            # Support both single and multiple port forwards
            forwards = config.get('forwards', [])
            if forwards:
                # Multiple port forwards
                for forward in forwards:
                    remote_port = forward.get('remote_port', '')
                    remote_host = forward.get('remote_host', '')
                    local_port = forward.get('local_port', '')
                    tunnel_spec = f"{remote_port}:{remote_host}:{local_port}"
                    cmd.extend(['-R', tunnel_spec])
            else:
                # Legacy single port forward (backward compatibility)
                local_port = config.get('local_port', '')
                remote_host = config.get('remote_host', '')
                remote_port = config.get('remote_port', '')
                tunnel_spec = f"{remote_port}:{remote_host}:{local_port}"
                cmd.extend(['-R', tunnel_spec])
        else:  # dynamic
            # Dynamic port forwarding (SOCKS proxy): -D local_port
            local_port = config.get('local_port', '')
            cmd.extend(['-D', local_port])

        # Add SSH connection details; an empty port leaves it to ~/.ssh/config
        if ssh_port:
            cmd.extend(['-p', ssh_port])
        cmd.append(f"{ssh_user}@{ssh_host}" if ssh_user else ssh_host)

        try:
            process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except Exception as e:
            self.messages[tunnel_id] = str(e)
            return False, str(e)
        self.tunnels[tunnel_id] = process
        self.messages[tunnel_id] = ''
        threading.Thread(target=self._read_stderr, args=(tunnel_id, process), daemon=True).start()
        return True, "Tunnel started successfully"

    def stop_tunnel(self, tunnel_id):
        """Stop a running SSH tunnel; the tunnel goes Offline"""
        self.wanted.discard(tunnel_id)
        self.messages[tunnel_id] = ''
        if tunnel_id in self.tunnels:
            process = self.tunnels[tunnel_id]
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            del self.tunnels[tunnel_id]
            self.messages[tunnel_id] = ''
            return True, "Tunnel stopped"
        return False, "Tunnel not found"

    def is_running(self, tunnel_id):
        """Check if a tunnel is currently running"""
        if tunnel_id in self.tunnels:
            return self.tunnels[tunnel_id].poll() is None
        return False

    def own_pids(self):
        return {p.pid for p in self.tunnels.values() if p.poll() is None}

    def status(self, config):
        """Return (label, color, message) for a tunnel"""
        tunnel_id = config.get('id')
        port = tunnel_port(config)
        owner = self.port_owner(port)
        message = self.messages.get(tunnel_id, '')
        if self.is_running(tunnel_id):
            process = self.tunnels[tunnel_id]
            if not port or (owner and owner['pid'] == process.pid):
                return "Running", COLOR_GREEN, message
            return "Connecting", COLOR_YELLOW, message
        if owner and owner['proc'] == 'ssh':
            return "External", COLOR_ORANGE, message or f"opened outside the app (ssh pid {owner['pid']})"
        if owner and not message:
            who = f"{owner['proc']} (pid {owner['pid']})" if owner['pid'] else "another user"
            message = f"port {port} in use by {who}"
        if tunnel_id in self.wanted:
            # Switched on, but the connection went down
            return "Stopped", COLOR_RED, message or "connection went down"
        return "Offline", COLOR_GREY, message

    def stop_external(self, config):
        """Close a tunnel port held by an ssh process outside this app"""
        port = tunnel_port(config)
        for entry in self.scanner.external_tunnels(self.own_pids()):
            if entry['port'] != port:
                continue
            if entry['control_path']:
                if entry['type'] != 'dynamic':
                    return False, (f"port {port} is a forward on a shared ssh connection to "
                                   f"{entry['host']}; close it with ssh -O cancel -L <spec>")
                result = subprocess.run(['ssh', '-S', entry['control_path'], '-O', 'cancel',
                                         '-D', port, entry['host']],
                                        capture_output=True, text=True, timeout=10)
                self.scanner._stamp = 0.0
                if result.returncode != 0:
                    return False, result.stderr.strip() or "ssh -O cancel failed"
                return True, f"Closed port {port} on the shared ssh connection"
            try:
                os.kill(entry['pid'], signal.SIGTERM)
            except OSError as e:
                return False, str(e)
            self.scanner._stamp = 0.0
            return True, f"Stopped ssh pid {entry['pid']}"
        return False, f"No external ssh process found on port {port}"

    def cleanup(self):
        """Stop all running tunnels"""
        for tunnel_id in list(self.tunnels.keys()):
            self.stop_tunnel(tunnel_id)


class ConfigManager:
    """Manages tunnel configuration persistence"""

    def __init__(self):
        self.config_dir = Path.home() / '.config' / APP_ID
        self.config_file = self.config_dir / 'tunnels.json'
        self.config_dir.mkdir(parents=True, exist_ok=True)
        # First run: take over the tunnels of Easy SSH Tunnel Manager
        legacy = Path.home() / '.config' / 'easy-ssh-tunnel' / 'tunnels.json'
        if not self.config_file.exists() and legacy.exists():
            shutil.copy(legacy, self.config_file)

    def load_tunnels(self):
        """Load saved tunnel configurations, giving each an id and a color"""
        tunnels = []
        if self.config_file.exists():
            try:
                with open(self.config_file, 'r') as f:
                    tunnels = json.load(f)
            except Exception as e:
                print(f"Error loading config: {e}")
                return []
        changed = False
        for index, config in enumerate(tunnels):
            if not config.get('id'):
                config['id'] = uuid.uuid4().hex
                changed = True
            if not normalize_hex(config.get('color')):
                config['color'] = TUNNEL_PALETTE[index % len(TUNNEL_PALETTE)]
                changed = True
        if changed:
            self.save_tunnels(tunnels)
        return tunnels

    def save_tunnels(self, tunnels):
        """Save tunnel configurations"""
        try:
            with open(self.config_file, 'w') as f:
                json.dump(tunnels, f, indent=2)
            return True
        except Exception as e:
            print(f"Error saving config: {e}")
            return False

    @staticmethod
    def new_tunnel_fields(existing):
        """id and next palette color for a tunnel being added"""
        return {'id': uuid.uuid4().hex,
                'color': TUNNEL_PALETTE[len(existing) % len(TUNNEL_PALETTE)]}


# Terminals and the arguments that make them run a command, in order of preference
# after $TERMINAL and the distribution's default (x-terminal-emulator, xdg-terminal-exec)
TERMINALS = [
    ('ptyxis', ['--']),            # Fedora 41+, Ubuntu 25.10+
    ('kgx', ['--']),               # GNOME Console
    ('gnome-terminal', ['--']),
    ('konsole', ['-e']),           # KDE
    ('xfce4-terminal', ['-x']),
    ('mate-terminal', ['-x']),
    ('lxterminal', ['-e']),
    ('qterminal', ['-e']),
    ('terminator', ['-x']),
    ('tilix', ['-e']),
    ('alacritty', ['-e']),
    ('kitty', []),
    ('wezterm', ['start', '--']),
    ('foot', []),
    ('xterm', ['-e']),
]
# Desktop -> terminals to try first
DESKTOP_TERMINALS = {
    'GNOME': ['ptyxis', 'kgx', 'gnome-terminal'],
    'KDE': ['konsole'],
    'XFCE': ['xfce4-terminal'],
    'MATE': ['mate-terminal'],
    'LXQT': ['qterminal'],
    'LXDE': ['lxterminal'],
}


def terminal_command():
    """Command prefix that runs a program in the default terminal"""
    env_terminal = os.environ.get('TERMINAL', '').strip()
    if env_terminal and shutil.which(env_terminal):
        known = dict(TERMINALS)
        return [env_terminal] + known.get(os.path.basename(env_terminal), ['-e'])
    if shutil.which('x-terminal-emulator'):
        return ['x-terminal-emulator', '-e']
    if shutil.which('xdg-terminal-exec'):
        return ['xdg-terminal-exec']
    known = dict(TERMINALS)
    preferred = []
    for desktop in os.environ.get('XDG_CURRENT_DESKTOP', '').upper().split(':'):
        preferred += DESKTOP_TERMINALS.get(desktop, [])
    for name in preferred + [name for name, _ in TERMINALS]:
        if shutil.which(name):
            return [name] + known[name]
    return ['xterm', '-e']


def tray_available():
    """True when a StatusNotifier host (system tray) runs in this session"""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = bus.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus',
                              'org.freedesktop.DBus', 'NameHasOwner',
                              GLib.Variant('(s)', ('org.kde.StatusNotifierWatcher',)),
                              GLib.VariantType('(b)'), Gio.DBusCallFlags.NONE, 2000, None)
        return reply.unpack()[0]
    except GLib.Error:
        return False


def color_dot_pixbuf(hex_color, size=16):
    """Round color swatch for menus"""
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    ctx = cairo.Context(surface)
    rgba = Gdk.RGBA()
    rgba.parse(normalize_hex(hex_color) or COLOR_DIM)
    ctx.set_source_rgba(rgba.red, rgba.green, rgba.blue, 1.0)
    ctx.arc(size / 2, size / 2, size / 2 - 1, 0, 6.2832)
    ctx.fill()
    return Gdk.pixbuf_get_from_surface(surface, 0, 0, size, size)


class SSHCommandParser:
    """Parses SSH commands and converts them to tunnel configurations"""

    @staticmethod
    def parse_ssh_command(command):
        """
        Parse an SSH command and extract tunnel configuration.
        Supports formats like:
        - ssh -L 8080:localhost:80 user@host
        - ssh -L 27017:mongodb-0:27017 -L 27018:mongodb-1:27017 user@host
        - ssh -R 9090:localhost:8080 -p 2222 user@host
        - ssh -D 1080 user@host
        """
        # Remove leading/trailing whitespace and normalize whitespace
        command = ' '.join(command.split())

        # Basic validation - must start with ssh
        if not command.startswith('ssh'):
            raise ValueError("Command must start with 'ssh'")

        # Initialize config with defaults
        config = {
            'type': 'local',
            'ssh_port': '22',
            'local_port': '',
            'remote_host': 'localhost',
            'remote_port': '',
            'ssh_user': '',
            'ssh_host': '',
            'name': '',
            'forwards': []
        }

        # Parse all -L (local forwarding) flags
        local_matches = re.findall(r'-L\s+(\d+):([^:\s]+):(\d+)', command)
        if local_matches:
            config['type'] = 'local'
            if len(local_matches) == 1:
                # Single forward - use legacy format for backward compatibility
                config['local_port'] = local_matches[0][0]
                config['remote_host'] = local_matches[0][1]
                config['remote_port'] = local_matches[0][2]
            else:
                # Multiple forwards - use new format
                config['forwards'] = []
                for match in local_matches:
                    config['forwards'].append({
                        'local_port': match[0],
                        'remote_host': match[1],
                        'remote_port': match[2]
                    })
                # Set first forward's local_port for display purposes
                config['local_port'] = local_matches[0][0]

        # Parse all -R (remote forwarding) flags
        remote_matches = re.findall(r'-R\s+(\d+):([^:\s]+):(\d+)', command)
        if remote_matches:
            config['type'] = 'remote'
            if len(remote_matches) == 1:
                # Single forward - use legacy format for backward compatibility
                config['remote_port'] = remote_matches[0][0]
                config['remote_host'] = remote_matches[0][1]
                config['local_port'] = remote_matches[0][2]
            else:
                # Multiple forwards - use new format
                config['forwards'] = []
                for match in remote_matches:
                    config['forwards'].append({
                        'remote_port': match[0],
                        'remote_host': match[1],
                        'local_port': match[2]
                    })
                # Set first forward's remote_port for display purposes
                config['remote_port'] = remote_matches[0][0]

        # Parse -D (dynamic forwarding)
        dynamic_match = re.search(r'-D\s+(\d+)', command)
        if dynamic_match:
            config['type'] = 'dynamic'
            config['local_port'] = dynamic_match.group(1)

        # Parse -p (SSH port)
        port_match = re.search(r'-p\s+(\d+)', command)
        if port_match:
            config['ssh_port'] = port_match.group(1)

        # Destination: [user@]host, the first non-option argument
        parsed = parse_ssh_argv(command.split())
        if not parsed['host']:
            raise ValueError("Could not find a host in command")
        config['ssh_user'] = parsed['user']
        config['ssh_host'] = parsed['host']
        if not port_match:
            # No -p: leave the port to ~/.ssh/config
            config['ssh_port'] = ''

        # Generate a default name
        if config['type'] == 'local':
            if config.get('forwards'):
                # Multiple forwards - show count in name
                config['name'] = f"{config['ssh_host']}_L{len(config['forwards'])}x"
            else:
                config['name'] = f"{config['ssh_host']}_L{config['local_port']}"
        elif config['type'] == 'remote':
            if config.get('forwards'):
                # Multiple forwards - show count in name
                config['name'] = f"{config['ssh_host']}_R{len(config['forwards'])}x"
            else:
                config['name'] = f"{config['ssh_host']}_R{config['remote_port']}"
        else:  # dynamic
            config['name'] = f"{config['ssh_host']}_D{config['local_port']}"

        return config

    @staticmethod
    def export_to_command(config):
        """
        Convert a tunnel configuration to an SSH command line.
        """
        tunnel_type = config.get('type', 'local')
        ssh_host = config.get('ssh_host', '')
        ssh_user = config.get('ssh_user', '')
        ssh_port = config.get('ssh_port', '22')

        # Build SSH command
        cmd = "ssh -N"

        if tunnel_type == 'local':
            forwards = config.get('forwards', [])
            if forwards:
                # Multiple port forwards
                for forward in forwards:
                    local_port = forward.get('local_port', '')
                    remote_host = forward.get('remote_host', 'localhost')
                    remote_port = forward.get('remote_port', '')
                    tunnel_spec = f"{local_port}:{remote_host}:{remote_port}"
                    cmd += f" -L {tunnel_spec}"
            else:
                # Legacy single port forward
                local_port = config.get('local_port', '')
                remote_host = config.get('remote_host', 'localhost')
                remote_port = config.get('remote_port', '')
                tunnel_spec = f"{local_port}:{remote_host}:{remote_port}"
                cmd += f" -L {tunnel_spec}"
        elif tunnel_type == 'remote':
            forwards = config.get('forwards', [])
            if forwards:
                # Multiple port forwards
                for forward in forwards:
                    remote_port = forward.get('remote_port', '')
                    remote_host = forward.get('remote_host', 'localhost')
                    local_port = forward.get('local_port', '')
                    tunnel_spec = f"{remote_port}:{remote_host}:{local_port}"
                    cmd += f" -R {tunnel_spec}"
            else:
                # Legacy single port forward
                local_port = config.get('local_port', '')
                remote_host = config.get('remote_host', 'localhost')
                remote_port = config.get('remote_port', '')
                tunnel_spec = f"{remote_port}:{remote_host}:{local_port}"
                cmd += f" -R {tunnel_spec}"
        else:  # dynamic
            local_port = config.get('local_port', '')
            cmd += f" -D {local_port}"

        # Add SSH connection details
        if ssh_port and ssh_port != '22':
            cmd += f" -p {ssh_port}"
        cmd += f" {ssh_user}@{ssh_host}" if ssh_user else f" {ssh_host}"

        return cmd


class CellRendererSwitch(Gtk.CellRenderer):
    """ON/OFF pill switch drawn in a TreeView cell"""

    __gsignals__ = {'toggled': (GObject.SignalFlags.RUN_LAST, None, (str,))}
    active = GObject.Property(type=bool, default=False)

    WIDTH, HEIGHT = 58, 26

    def __init__(self):
        super().__init__()
        self.set_property('mode', Gtk.CellRendererMode.ACTIVATABLE)
        self.set_padding(6, 4)

    def do_get_preferred_width(self, widget):
        width = self.WIDTH + 2 * self.get_padding()[0]
        return width, width

    def do_get_preferred_height(self, widget):
        height = self.HEIGHT + 2 * self.get_padding()[1]
        return height, height

    def do_render(self, cr, widget, background_area, cell_area, flags):
        w, h = self.WIDTH, self.HEIGHT
        x = cell_area.x + (cell_area.width - w) / 2
        y = cell_area.y + (cell_area.height - h) / 2
        r = h / 2
        on = self.get_property('active')

        def rgb(hex_color):
            rgba = Gdk.RGBA()
            rgba.parse(hex_color)
            return rgba.red, rgba.green, rgba.blue

        # Track
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -1.5708, 1.5708)
        cr.arc(x + r, y + r, r, 1.5708, 4.7124)
        cr.close_path()
        cr.set_source_rgb(*rgb("#34c759" if on else "#3d424d"))
        cr.fill()

        # Label
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(10)
        text = "ON" if on else "OFF"
        extents = cr.text_extents(text)
        text_x = x + 8 if on else x + w - 8 - extents.width
        cr.move_to(text_x - extents.x_bearing, y + h / 2 - extents.y_bearing - extents.height / 2)
        cr.set_source_rgb(*rgb("#ffffff" if on else "#a0a3ad"))
        cr.show_text(text)

        # Knob
        knob_x = x + w - r if on else x + r
        cr.arc(knob_x, y + r, r - 3, 0, 6.2832)
        cr.set_source_rgb(*rgb("#ececec"))
        cr.fill()

    def do_activate(self, event, widget, path, background_area, cell_area, flags):
        self.emit('toggled', path)
        return True


class Swatch(Gtk.DrawingArea):
    """Clickable color circle or tile with a ring when selected"""

    def __init__(self, color, shape='circle', width=40, height=40, on_click=None,
                 on_double_click=None, plus=False):
        super().__init__()
        self.color, self.shape, self.plus = color, shape, plus
        self.selected = self.hover = False
        self.on_click, self.on_double_click = on_click, on_double_click
        self.badge_click = None  # set for Your colors: removes the color
        self.show_badge = self.show_pencil = False
        self.set_size_request(width, height)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.ENTER_NOTIFY_MASK
                        | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.connect("draw", self._draw)
        self.connect("button-press-event", self._pressed)
        self.connect("enter-notify-event", lambda *_: self._set_hover(True))
        self.connect("leave-notify-event", lambda *_: self._set_hover(False))
        self.connect("realize", lambda w: w.get_window().set_cursor(
            Gdk.Cursor.new_from_name(w.get_display(), "pointer")))
        if color and not plus:
            self.set_tooltip_text(color)

    def set_color(self, color):
        self.color = color
        self.set_tooltip_text(color)
        self.queue_draw()

    def set_selected(self, selected):
        if selected != self.selected:
            self.selected = selected
            self.queue_draw()

    def _set_hover(self, hover):
        self.hover = hover
        self.queue_draw()

    BADGE_CENTER, BADGE_RADIUS = 9.5, 8.5

    def set_marks(self, badge, pencil):
        if (badge, pencil) != (self.show_badge, self.show_pencil):
            self.show_badge, self.show_pencil = badge, pencil
            self.queue_draw()

    def _pressed(self, widget, event):
        if event.button != 1:
            return False
        if (self.show_badge and self.badge_click and event.type == Gdk.EventType.BUTTON_PRESS
                and math.hypot(event.x - self.BADGE_CENTER, event.y - self.BADGE_CENTER)
                <= self.BADGE_RADIUS + 2):
            self.badge_click(self.color)
            return True
        if event.type == Gdk.EventType._2BUTTON_PRESS and self.on_double_click:
            self.on_double_click(self.color)
        elif event.type == Gdk.EventType.BUTTON_PRESS and self.on_click:
            self.on_click(self.color)
        return True

    def _shape(self, cr, inset):
        width, height = self.get_allocated_width(), self.get_allocated_height()
        if self.shape == 'circle':
            radius = min(width, height) / 2 - inset
            cr.arc(width / 2, height / 2, radius, 0, 6.2832)
            return
        radius = 10
        x, y, w, h = inset, inset, width - 2 * inset, height - 2 * inset
        cr.new_sub_path()
        cr.arc(x + w - radius, y + radius, radius, -1.5708, 0)
        cr.arc(x + w - radius, y + h - radius, radius, 0, 1.5708)
        cr.arc(x + radius, y + h - radius, radius, 1.5708, 3.1416)
        cr.arc(x + radius, y + radius, radius, 3.1416, 4.7124)
        cr.close_path()

    def _draw(self, widget, cr):
        rgba = Gdk.RGBA()
        rgba.parse("#a3a6ad" if self.plus else (normalize_hex(self.color) or COLOR_DIM))
        # Circles leave a dark gap between fill and ring, tiles get a frame on the edge
        self._shape(cr, 6 if self.shape == 'circle' else 3)
        cr.set_source_rgba(rgba.red, rgba.green, rgba.blue, 1)
        cr.fill()
        if self.plus:
            width, height = self.get_allocated_width(), self.get_allocated_height()
            cr.set_source_rgb(0.13, 0.13, 0.15)
            cr.set_line_width(2.5)
            arm = min(width, height) / 5
            cr.move_to(width / 2 - arm, height / 2)
            cr.line_to(width / 2 + arm, height / 2)
            cr.move_to(width / 2, height / 2 - arm)
            cr.line_to(width / 2, height / 2 + arm)
            cr.stroke()
        if self.show_pencil:
            # The theme's edit icon, centered, in a darker shade: click again to change the color
            width, height = self.get_allocated_width(), self.get_allocated_height()
            shade = Gdk.RGBA()
            shade.red, shade.green, shade.blue, shade.alpha = (
                rgba.red * 0.45, rgba.green * 0.45, rgba.blue * 0.45, 1.0)
            info = Gtk.IconTheme.get_default().lookup_icon("document-edit-symbolic", 18, 0)
            if info is not None:
                icon, _ = info.load_symbolic(shade, None, None, None)
                Gdk.cairo_set_source_pixbuf(cr, icon, (width - icon.get_width()) / 2,
                                            (height - icon.get_height()) / 2)
                cr.paint()
        if self.selected or self.hover:
            line = 3 if self.selected else 2
            self._shape(cr, line / 2)
            cr.set_source_rgba(1, 1, 1, 1 if self.selected else 0.35)
            cr.set_line_width(line)
            cr.stroke()
        if self.show_badge:
            # Grey minus badge in the top-left corner: removes the color
            cr.arc(self.BADGE_CENTER, self.BADGE_CENTER, self.BADGE_RADIUS, 0, 6.2832)
            cr.set_source_rgb(0.27, 0.28, 0.30)
            cr.fill()
            cr.set_source_rgb(0.85, 0.86, 0.88)
            cr.set_line_width(2)
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.move_to(self.BADGE_CENTER - 4, self.BADGE_CENTER)
            cr.line_to(self.BADGE_CENTER + 4, self.BADGE_CENTER)
            cr.stroke()
        return True


class ColorPickerDialog(Gtk.Dialog):
    """Theme presets, your colors and recent colors on the left; shades of the pick on the right"""

    COLUMNS = 6

    def __init__(self, parent, color, row, memory):
        super().__init__(title="Pick a Color", transient_for=parent, modal=True, use_header_bar=True)
        self.get_style_context().add_class("color-picker")
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.memory = memory
        self.color = normalize_hex(color) or BASE_COLORS[0]
        self.row = list(row) if row and self.color in row else memory.row_for(self.color)
        self.swatches = []

        content = self.get_content_area()
        content.set_border_width(22)
        # Grid: sections on the left; shades next to presets and your colors; the hex value
        # and Select next to the two rows of recent colors
        layout = Gtk.Grid(column_spacing=32, row_spacing=12)
        content.pack_start(layout, True, True, 0)

        layout.attach(self._title("THEME PRESETS"), 0, 0, 1, 1)
        layout.attach(self._grid(BASE_COLORS, keep_slot=True), 0, 1, 1, 1)
        # Removing colors only while "Edit" is on, so nothing goes by accident
        mine_header = Gtk.Box(spacing=10)
        mine_header.set_margin_top(12)
        mine_header.pack_start(self._title("YOUR COLORS"), False, False, 0)
        self.edit_toggle = Gtk.ToggleButton(label="\u2212 Edit")
        self.edit_toggle.set_relief(Gtk.ReliefStyle.NONE)
        self.edit_toggle.get_style_context().add_class("edit-toggle")
        self.edit_toggle.connect("toggled", self.on_edit_toggled)
        mine_header.pack_start(self.edit_toggle, False, False, 0)
        layout.attach(mine_header, 0, 2, 1, 1)
        self.mine_holder = Gtk.Box()
        layout.attach(self.mine_holder, 0, 3, 1, 1)
        recent_title = self._title("RECENT")
        recent_title.set_margin_top(12)
        layout.attach(recent_title, 0, 4, 1, 1)
        recent = self._grid(memory.recent, empty="Colors you save on a tunnel show up here")
        # Room for two rows, also while there are fewer recent colors
        recent.set_size_request(-1, 2 * 46 + 8)
        layout.attach(recent, 0, 5, 1, 1)
        self._fill_mine()

        shades = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, homogeneous=True)
        layout.attach(shades, 1, 0, 1, 5)
        self.tiles = []
        for _ in range(SHADE_COUNT):
            tile = Swatch(None, shape='tile', width=168, height=40,
                          on_click=lambda c: self.choose(c, keep_row=True),
                          on_double_click=self._choose_and_close)
            shades.pack_start(tile, True, True, 0)
            self.tiles.append(tile)

        # Hex value of the selection, editable, and Select
        actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        actions.set_valign(Gtk.Align.START)
        layout.attach(actions, 1, 5, 1, 1)
        self.hex_entry = Gtk.Entry()
        self.hex_entry.get_style_context().add_class("hex-entry")
        self.hex_entry.set_width_chars(9)
        self.hex_entry.set_alignment(0.5)
        self.hex_entry.connect("changed", self.on_hex_typed)
        self.hex_entry.connect("activate", lambda _e: self.response(Gtk.ResponseType.OK))
        actions.pack_start(self.hex_entry, False, False, 0)
        select = Gtk.Button(label="Select")
        select.get_style_context().add_class("suggested-action")
        select.get_style_context().add_class("select-button")
        select.connect("clicked", lambda _b: self.response(Gtk.ResponseType.OK))
        actions.pack_start(select, False, False, 0)
        self.choose(self.color, keep_row=True)
        # Focus Select once the window is shown, so a keypress does not overwrite the hex value
        self.connect("map", lambda *_: GLib.idle_add(self._focus_select, select))
        self.show_all()

    def _focus_select(self, select):
        select.grab_focus()
        self.hex_entry.set_position(-1)
        return False

    @staticmethod
    def _title(text):
        label = Gtk.Label(xalign=0)
        label.set_markup(f'<span size="11000" weight="600" letter_spacing="2200" '
                         f'foreground="#9da0a8">{text}</span>')
        return label

    def _grid(self, colors, plus=False, empty=None, keep_slot=False, mine=False):
        """Swatch grid. keep_slot: clicking switches to that color's shades but stays on the
        same tile (presets and your colors); otherwise the exact color is selected (recent)"""
        grid = Gtk.Grid(row_spacing=8, column_spacing=10)
        items = list(colors) + (['+'] if plus else [])
        for index, color in enumerate(items):
            if color == '+':
                swatch = Swatch(None, plus=True, width=46, height=46,
                                on_click=lambda _c: self.on_add_custom())
                swatch.set_tooltip_text("Add a custom color")
            else:
                if mine:
                    # Your colors: a click switches to it, a click on the selected one edits it,
                    # the minus badge removes it
                    swatch = Swatch(color, width=46, height=46,
                                    on_click=lambda _c, base=color: self.on_mine_clicked(base))
                    swatch.badge_click = lambda _c, base=color: self.on_mine_removed(base)
                elif keep_slot:
                    # Shows the shade of the selected tile, but switches by its base color
                    swatch = Swatch(color, width=46, height=46,
                                    on_click=lambda _c, base=color: self.choose(base, keep_slot=True),
                                    on_double_click=lambda _c: self.response(Gtk.ResponseType.OK))
                else:
                    swatch = Swatch(color, width=46, height=46, on_click=self.choose,
                                    on_double_click=self._choose_and_close)
                swatch.keep_slot = keep_slot or mine
                swatch.mine = mine
                swatch.base = color
                self.swatches.append(swatch)
            grid.attach(swatch, index % self.COLUMNS, index // self.COLUMNS, 1, 1)
        if not items and empty:
            label = Gtk.Label(label=empty, xalign=0)
            label.get_style_context().add_class("dim-label")
            grid.attach(label, 0, 0, self.COLUMNS, 1)
        return grid

    def _fill_mine(self):
        for child in self.mine_holder.get_children():
            self.swatches = [s for s in self.swatches if s.get_parent() is not child]
            self.mine_holder.remove(child)
        mine = self._grid(self.memory.mine, plus=True, mine=True)
        # Room for two rows: 11 colors and the + button
        mine.set_size_request(-1, 2 * 46 + 8)
        self.mine_holder.pack_start(mine, False, False, 0)
        self.mine_holder.show_all()

    def choose(self, color, keep_row=False, keep_slot=False):
        """Select a color; a color from outside the current shades brings its own shade row.
        With keep_slot the selection stays on the same tile of the new row."""
        color = normalize_hex(color)
        if not color:
            return
        slot = self.row.index(self.color) if self.color in self.row else MIDDLE_TINT
        if not (keep_row and color in self.row):
            self.row = self.memory.row_for(color)
        if keep_slot:
            color = self.row[slot]
        self.color = color
        for tile, shade in zip(self.tiles, self.row):
            tile.set_color(shade)
            tile.set_selected(shade == color)
        slot = self.row.index(color) if color in self.row else MIDDLE_TINT
        for swatch in self.swatches:
            if getattr(swatch, 'keep_slot', False):
                # Presets and your colors show their shade on the selected tile and stand for
                # their whole row
                swatch.set_color(self._row_of(swatch.base)[slot])
                swatch.set_selected(swatch.base in self.row)
                if getattr(swatch, 'mine', False):
                    editing = self.edit_toggle.get_active()
                    swatch.set_marks(badge=editing and not swatch.selected, pencil=swatch.selected)
            else:
                swatch.set_selected(swatch.color == color)
        if normalize_hex(self.hex_entry.get_text()) != color:
            self.hex_entry.set_text(color)
            self.hex_entry.select_region(-1, -1)

    def on_hex_typed(self, entry):
        color = normalize_hex(entry.get_text())
        entry.get_style_context().remove_class("error")
        if not color:
            entry.get_style_context().add_class("error")
        elif color != self.color:
            self.choose(color, keep_row=True)

    def _row_of(self, base):
        cache = self.__dict__.setdefault('_row_cache', {})
        if base not in cache:
            cache[base] = self.memory.row_for(base)
        return cache[base]

    def _choose_and_close(self, color):
        self.choose(color, keep_row=True)
        self.response(Gtk.ResponseType.OK)

    def on_edit_toggled(self, button):
        button.set_label("Done" if button.get_active() else "\u2212 Edit")
        self.choose(self.color, keep_row=True)

    def on_mine_clicked(self, base):
        if base in self.row:
            self.on_edit_mine(base)
        else:
            self.choose(base, keep_slot=True)

    def on_mine_removed(self, base):
        self.memory.remove_mine(base)
        self._fill_mine()
        self.choose(self.color, keep_row=True)

    def _ask_color(self, title, start):
        """GTK's color editor; returns the picked hex or None"""
        chooser = Gtk.ColorChooserDialog(title=title, transient_for=self)
        chooser.set_use_alpha(False)
        chooser.set_property("show-editor", True)
        rgba = Gdk.RGBA()
        rgba.parse(start)
        chooser.set_rgba(rgba)
        picked = None
        if chooser.run() == Gtk.ResponseType.OK:
            value = chooser.get_rgba()
            picked = "#{:02x}{:02x}{:02x}".format(
                round(value.red * 255), round(value.green * 255), round(value.blue * 255))
        chooser.destroy()
        return picked

    def on_edit_mine(self, base):
        """Change the base of an entry in Your colors, keeping its place"""
        color = self._ask_color("Change Color", base)
        if not color:
            return
        self.memory.replace_mine(base, color)
        self.__dict__.pop('_row_cache', None)
        self._fill_mine()
        self.choose(color)

    def on_add_custom(self):
        """GTK's color editor for a custom color, added to Your colors"""
        chooser = Gtk.ColorChooserDialog(title="Custom Color", transient_for=self)
        chooser.set_use_alpha(False)
        chooser.set_property("show-editor", True)
        rgba = Gdk.RGBA()
        rgba.parse(self.color)
        chooser.set_rgba(rgba)
        if chooser.run() == Gtk.ResponseType.OK:
            picked = chooser.get_rgba()
            color = "#{:02x}{:02x}{:02x}".format(
                round(picked.red * 255), round(picked.green * 255), round(picked.blue * 255))
            self.memory.add_mine(color)
            self._fill_mine()
            self.choose(color)
        chooser.destroy()

    def result(self):
        return self.color, list(self.row)


class TunnelDialog(Gtk.Dialog):
    """Dialog for adding/editing SSH tunnel configurations"""

    def __init__(self, parent, tunnel_data=None, color_memory=None, tint_row_hint=None):
        super().__init__(title="SSH Tunnel Configuration", parent=parent)
        self.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OK, Gtk.ResponseType.OK
        )

        self.set_default_size(400, 400)
        box = self.get_content_area()
        box.set_spacing(6)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)

        # Tunnel name
        box.pack_start(Gtk.Label(label="Tunnel Name:", xalign=0), False, False, 0)
        self.name_entry = Gtk.Entry()
        box.pack_start(self.name_entry, False, False, 0)

        # Tunnel color: a tile that opens the color picker, and the hex value
        box.pack_start(Gtk.Label(label="Color:", xalign=0), False, False, 6)
        color_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.color_memory = color_memory or ColorMemory(Path.home() / '.config' / APP_ID)
        self.tint_row = list(tint_row_hint or [])
        self._tint_row_hint = list(self.tint_row)
        self.color_swatch = Swatch(TUNNEL_PALETTE[0], shape='tile', width=64, height=38,
                                   on_click=lambda _c: self.open_color_picker())
        self.color_swatch.set_tooltip_text("Pick a color")
        color_box.pack_start(self.color_swatch, False, False, 0)
        self.color_entry = Gtk.Entry()
        self.color_entry.set_placeholder_text("#7eb26d")
        self.color_entry.set_width_chars(10)
        self.color_entry.connect("changed", self.on_color_typed)
        color_box.pack_start(self.color_entry, False, False, 0)
        box.pack_start(color_box, False, False, 0)
        self.set_color(TUNNEL_PALETTE[0])

        # Tunnel type
        box.pack_start(Gtk.Label(label="Tunnel Type:", xalign=0), False, False, 6)
        self.type_combo = Gtk.ComboBoxText()
        self.type_combo.append("local", "Local (-L) - Forward local port to remote")
        self.type_combo.append("remote", "Remote (-R) - Forward remote port to local")
        self.type_combo.append("dynamic", "Dynamic (-D) - SOCKS proxy")
        self.type_combo.set_active(0)
        self.type_combo.connect("changed", self.on_type_changed)
        box.pack_start(self.type_combo, False, False, 0)

        # SSH connection details
        box.pack_start(Gtk.Label(label="SSH Connection:", xalign=0), False, False, 6)

        ssh_grid = Gtk.Grid()
        ssh_grid.set_column_spacing(6)
        ssh_grid.set_row_spacing(6)

        ssh_grid.attach(Gtk.Label(label="User:", xalign=0), 0, 0, 1, 1)
        self.ssh_user_entry = Gtk.Entry()
        self.ssh_user_entry.set_placeholder_text("from ~/.ssh/config")
        ssh_grid.attach(self.ssh_user_entry, 1, 0, 1, 1)

        ssh_grid.attach(Gtk.Label(label="Host:", xalign=0), 0, 1, 1, 1)
        self.ssh_host_entry = Gtk.Entry()
        self.ssh_host_entry.set_placeholder_text("example.com")
        ssh_grid.attach(self.ssh_host_entry, 1, 1, 1, 1)

        ssh_grid.attach(Gtk.Label(label="Port:", xalign=0), 0, 2, 1, 1)
        self.ssh_port_entry = Gtk.Entry()
        self.ssh_port_entry.set_placeholder_text("from ~/.ssh/config")
        ssh_grid.attach(self.ssh_port_entry, 1, 2, 1, 1)

        box.pack_start(ssh_grid, False, False, 0)

        # Tunnel details
        box.pack_start(Gtk.Label(label="Tunnel Details:", xalign=0), False, False, 6)

        self.tunnel_grid = Gtk.Grid()
        self.tunnel_grid.set_column_spacing(6)
        self.tunnel_grid.set_row_spacing(6)

        self.local_port_label = Gtk.Label(label="Local Port:", xalign=0)
        self.tunnel_grid.attach(self.local_port_label, 0, 0, 1, 1)
        self.local_port_entry = Gtk.Entry()
        self.local_port_entry.set_placeholder_text("8080")
        self.tunnel_grid.attach(self.local_port_entry, 1, 0, 1, 1)

        self.remote_host_label = Gtk.Label(label="Remote Host:", xalign=0)
        self.tunnel_grid.attach(self.remote_host_label, 0, 1, 1, 1)
        self.remote_host_entry = Gtk.Entry()
        self.remote_host_entry.set_placeholder_text("localhost")
        self.tunnel_grid.attach(self.remote_host_entry, 1, 1, 1, 1)

        self.remote_port_label = Gtk.Label(label="Remote Port:", xalign=0)
        self.tunnel_grid.attach(self.remote_port_label, 0, 2, 1, 1)
        self.remote_port_entry = Gtk.Entry()
        self.remote_port_entry.set_placeholder_text("80")
        self.tunnel_grid.attach(self.remote_port_entry, 1, 2, 1, 1)

        box.pack_start(self.tunnel_grid, False, False, 0)

        # Let the fields use the dialog width so long host names stay readable
        for entry in (self.ssh_user_entry, self.ssh_host_entry, self.ssh_port_entry,
                      self.local_port_entry, self.remote_host_entry, self.remote_port_entry):
            entry.set_hexpand(True)

        # Multi-forward info label (shown when editing tunnels with multiple forwards)
        self.multi_forward_label = Gtk.Label()
        self.multi_forward_label.set_markup("<b>Note:</b> This tunnel has multiple port forwards.\nTo edit them, delete this tunnel and re-import the SSH command.")
        self.multi_forward_label.set_xalign(0)
        self.multi_forward_label.set_line_wrap(True)
        self.multi_forward_label.set_no_show_all(True)
        box.pack_start(self.multi_forward_label, False, False, 6)

        # Load existing data if editing
        self.tunnel_data = tunnel_data
        if tunnel_data:
            self.load_data(tunnel_data)

        self.on_type_changed(self.type_combo)
        self.show_all()

    def on_type_changed(self, combo):
        """Update UI based on selected tunnel type"""
        tunnel_type = combo.get_active_id()

        if tunnel_type == "local":
            self.local_port_label.set_text("Local Port:")
            self.local_port_entry.set_placeholder_text("8080")
            self.remote_host_label.show()
            self.remote_host_entry.show()
            self.remote_port_label.set_text("Remote Port:")
            self.remote_port_entry.set_placeholder_text("80")
            self.remote_port_label.show()
            self.remote_port_entry.show()
        elif tunnel_type == "remote":
            self.local_port_label.set_text("Local Port:")
            self.local_port_entry.set_placeholder_text("8080")
            self.remote_host_label.show()
            self.remote_host_entry.show()
            self.remote_port_label.set_text("Remote Port:")
            self.remote_port_entry.set_placeholder_text("9090")
            self.remote_port_label.show()
            self.remote_port_entry.show()
        else:  # dynamic
            self.local_port_label.set_text("SOCKS Port:")
            self.local_port_entry.set_placeholder_text("1080")
            self.remote_host_label.hide()
            self.remote_host_entry.hide()
            self.remote_port_label.hide()
            self.remote_port_entry.hide()

    def set_color(self, hex_color):
        self.color_entry.set_text(hex_color)
        self.on_color_typed(self.color_entry)

    def on_color_typed(self, entry):
        hex_color = normalize_hex(entry.get_text())
        entry.get_style_context().remove_class("error")
        if not hex_color:
            entry.get_style_context().add_class("error")
            return
        self.color_swatch.set_color(hex_color)
        self.color_swatch.set_tooltip_text("Pick a color")
        if hex_color not in self.tint_row:
            if hex_color in self._tint_row_hint:
                self.tint_row = list(self._tint_row_hint)
            else:
                self.tint_row = self.color_memory.row_for(hex_color)

    def open_color_picker(self):
        current = normalize_hex(self.color_entry.get_text()) or TUNNEL_PALETTE[0]
        picker = ColorPickerDialog(self, current, self.tint_row, self.color_memory)
        if picker.run() == Gtk.ResponseType.OK:
            color, row = picker.result()
            self.tint_row = row
            self._tint_row_hint = list(row)
            self.set_color(color)
        picker.destroy()

    def load_data(self, data):
        """Load tunnel data into the form"""
        self.name_entry.set_text(data.get('name', ''))
        self.set_color(normalize_hex(data.get('color')) or TUNNEL_PALETTE[0])
        self.type_combo.set_active_id(data.get('type', 'local'))
        self.ssh_user_entry.set_text(data.get('ssh_user', ''))
        self.ssh_host_entry.set_text(data.get('ssh_host', ''))
        self.ssh_port_entry.set_text(data.get('ssh_port', '22'))
        self.local_port_entry.set_text(data.get('local_port', ''))
        self.remote_host_entry.set_text(data.get('remote_host', ''))
        self.remote_port_entry.set_text(data.get('remote_port', ''))

        # Check if this has multiple forwards
        forwards = data.get('forwards', [])
        if forwards and len(forwards) > 0:
            # Show the multi-forward info label
            self.multi_forward_label.show()
            # Make fields read-only to prevent confusion
            self.local_port_entry.set_editable(False)
            self.remote_host_entry.set_editable(False)
            self.remote_port_entry.set_editable(False)

    def missing_fields(self):
        """Labels of required fields that are empty"""
        data = self.get_data()
        missing = []
        if not data['name']:
            missing.append("Tunnel Name")
        if not data['ssh_host']:
            missing.append("Host")
        if not data.get('forwards'):
            port_label = "SOCKS Port" if data['type'] == 'dynamic' else "Local Port"
            if not data['local_port']:
                missing.append(port_label)
            if data['type'] in ('local', 'remote'):
                if not data['remote_host']:
                    missing.append("Remote Host")
                if not data['remote_port']:
                    missing.append("Remote Port")
        return missing

    def run_until_valid(self):
        """Run the dialog; on OK with missing fields, warn and keep the dialog open"""
        while True:
            response = self.run()
            if response != Gtk.ResponseType.OK:
                return response
            missing = self.missing_fields()
            if not missing:
                return response
            warning = Gtk.MessageDialog(
                parent=self, flags=0, message_type=Gtk.MessageType.WARNING,
                buttons=Gtk.ButtonsType.OK, text="Some required fields are empty")
            warning.format_secondary_text("Fill in: " + ", ".join(missing))
            warning.run()
            warning.destroy()

    def get_data(self):
        """Get tunnel data from the form"""
        data = {
            'name': self.name_entry.get_text(),
            'type': self.type_combo.get_active_id(),
            'ssh_user': self.ssh_user_entry.get_text(),
            'ssh_host': self.ssh_host_entry.get_text(),
            'ssh_port': self.ssh_port_entry.get_text(),
            'local_port': self.local_port_entry.get_text(),
            'remote_host': self.remote_host_entry.get_text(),
            'remote_port': self.remote_port_entry.get_text(),
            'color': normalize_hex(self.color_entry.get_text()) or TUNNEL_PALETTE[0],
        }
        for key in ('name', 'ssh_user', 'ssh_host', 'ssh_port', 'local_port',
                    'remote_host', 'remote_port'):
            data[key] = data[key].strip()

        # Preserve id and forwards if they exist in the original data
        if self.tunnel_data and self.tunnel_data.get('id'):
            data['id'] = self.tunnel_data['id']
        if self.tunnel_data and 'forwards' in self.tunnel_data:
            data['forwards'] = self.tunnel_data['forwards']

        return data


class EasySSHTunnelApp(Gtk.Window):
    """Main application window"""

    def __init__(self, app_indicator=None, tunnel_manager=None, config_manager=None):
        super().__init__(title=APP_NAME)
        self.set_default_size(1100, 420)
        self.set_border_width(10)

        # Set window properties for taskbar appearance
        # Note: set_wmclass is deprecated in GTK3 but may still be useful for some window managers
        # It helps window managers group windows correctly
        import warnings
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=DeprecationWarning)
            try:
                self.set_wmclass(APP_ID, APP_NAME)
            except:
                pass

        # Set window type hint to NORMAL to ensure taskbar visibility
        self.set_type_hint(Gdk.WindowTypeHint.NORMAL)

        # Set role for window manager identification
        self.set_role(f"{APP_ID}-main")

        # Ensure the window can be focused and appears in taskbar
        self.set_skip_taskbar_hint(False)
        self.set_skip_pager_hint(False)

        # Try to set icon if available
        try:
            # Try local directory first, then system installation directory
            script_dir = os.path.dirname(os.path.abspath(__file__))
            local_logo = os.path.join(script_dir, "icons", "logo.png")
            local_icon = os.path.join(script_dir, "icons", "easy-ssh-tunnel-white.png")
            system_logo = "/usr/local/share/easier-ssh-tunnel/icons/logo.png"
            system_icon = "/usr/local/share/easier-ssh-tunnel/icons/easy-ssh-tunnel-white.png"

            # Try logo.png first, then fallback to other icons
            if os.path.exists(local_logo):
                self.set_icon_from_file(local_logo)
            elif os.path.exists(system_logo):
                self.set_icon_from_file(system_logo)
            elif os.path.exists(local_icon):
                self.set_icon_from_file(local_icon)
            elif os.path.exists(system_icon):
                self.set_icon_from_file(system_icon)
            else:
                # Set icon name for fallback to theme
                self.set_icon_name("network-workgroup")
        except Exception as e:
            print(f"Could not set window icon: {e}")
            # Fallback to theme icon
            self.set_icon_name("network-workgroup")

        self.app_indicator = app_indicator
        self.tunnel_manager = tunnel_manager or SSHTunnelManager()
        self.config_manager = config_manager or ConfigManager()
        self.tunnels_config = self.config_manager.load_tunnels()
        self.color_memory = ColorMemory(self.config_manager.config_dir)
        self._install_manage_tunnel_css()

        # Main layout
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.add(vbox)

        # Toolbar
        toolbar = Gtk.Toolbar()
        toolbar.get_style_context().add_class(Gtk.STYLE_CLASS_PRIMARY_TOOLBAR)
        toolbar.get_style_context().add_class("manage-tunnel-toolbar")
        toolbar.set_style(Gtk.ToolbarStyle.BOTH_HORIZ)
        toolbar.set_icon_size(Gtk.IconSize.SMALL_TOOLBAR)
        toolbar.set_show_arrow(False)

        buttons = [
            ("Add", "list-add-symbolic", "toolbar-button-neutral", self.on_add_tunnel, None),
            ("Edit", "document-edit-symbolic", "toolbar-button-neutral", self.on_edit_tunnel, None),
            ("Duplicate", "edit-copy-symbolic", "toolbar-button-neutral", self.on_duplicate_tunnel,
             "Copy the selected tunnel into a new one"),
            ("Remove", "user-trash-symbolic", "toolbar-button-danger", self.on_remove_tunnel, None),
            None,
            ("Start all", "media-skip-forward-symbolic", "toolbar-button-success", self.on_start_all,
             "Start every tunnel that is not open yet"),
            ("Stop all", "process-stop-symbolic", "toolbar-button-danger", self.on_stop_all,
             "Stop every tunnel started by this app"),
            ("Start", "media-playback-start-symbolic", "toolbar-button-success", self.on_start_tunnel, None),
            ("Stop", "media-playback-stop-symbolic", "toolbar-button-danger", self.on_stop_tunnel, None),
            None,
            ("Terminal", "utilities-terminal-symbolic", "toolbar-button-neutral", self.on_open_terminal,
             "Open an ssh session to this host in the default terminal"),
            None,
            ("Import", "document-open-symbolic", "toolbar-button-neutral", self.on_import_command,
             "Import SSH command"),
            ("Export", "document-save-symbolic", "toolbar-button-neutral", self.on_export_commands,
             "Export all tunnels as SSH commands"),
        ]
        self.toolbar_buttons = {}
        for position, spec in enumerate(buttons):
            if spec is None:
                toolbar.insert(Gtk.SeparatorToolItem(), position)
                continue
            label, icon, variant, handler, tooltip = spec
            button = Gtk.ToolButton()
            button.set_label(label)
            button.set_icon_widget(self._create_toolbar_icon(icon))
            if tooltip:
                button.set_tooltip_text(tooltip)
            self._style_toolbar_button(button, variant)
            button.connect("clicked", handler)
            toolbar.insert(button, position)
            self.toolbar_buttons[label] = button

        vbox.pack_start(toolbar, False, False, 0)

        # Shown when the automatic scan finds tunnels opened outside the app
        self.scan_bar = Gtk.InfoBar()
        self.scan_bar.set_message_type(Gtk.MessageType.INFO)
        self.scan_bar.add_button("Review", Gtk.ResponseType.OK)
        self.scan_bar.add_button("Ignore", Gtk.ResponseType.CLOSE)
        self.scan_bar.connect("response", self.on_scan_bar_response)
        self.scan_label = Gtk.Label(xalign=0)
        self.scan_label.set_line_wrap(True)
        self.scan_bar.get_content_area().add(self.scan_label)
        self.scan_bar.set_no_show_all(True)
        self.scan_bar.get_content_area().show_all()
        self.ignored_external = set()  # (port, pid) the user dismissed
        vbox.pack_start(self.scan_bar, False, False, 0)

        # Tunnel list
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)

        # ListStore: name, type, ssh_host, local_port, status, status_color, message,
        # config, switch_on, color
        self.tunnel_store = Gtk.ListStore(str, str, str, str, str, str, str, object, bool, str)

        self.tunnel_view = Gtk.TreeView(model=self.tunnel_store)
        self.tunnel_view.get_style_context().add_class("tunnel-list")
        self.tunnel_view.connect("row-activated", self.on_row_activated)
        self.tunnel_view.connect("button-press-event", self.on_list_button_press)
        self._last_click = None
        # Terminal and Duplicate need a selected tunnel
        self.tunnel_view.get_selection().connect("changed", self.on_selection_changed)
        self.toolbar_buttons["Terminal"].set_sensitive(False)
        self.toolbar_buttons["Duplicate"].set_sensitive(False)

        # On/off switch per row
        renderer = CellRendererSwitch()
        renderer.connect("toggled", self.on_switch_toggled)
        self.switch_column = Gtk.TreeViewColumn("", renderer, active=8)
        self.tunnel_view.append_column(self.switch_column)

        renderer = Gtk.CellRendererText()
        renderer.set_property("text", "●")
        renderer.set_property("scale", 1.3)
        column = Gtk.TreeViewColumn("", renderer, foreground=9)
        self.tunnel_view.append_column(column)

        for title, index, min_width in (("Name", 0, 120), ("Type", 1, 70),
                                        ("SSH Host", 2, 150), ("Local Port", 3, 80)):
            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(title, renderer, text=index)
            if title == "Name":
                # Name in the tunnel's own color
                column.add_attribute(renderer, "foreground", 9)
                renderer.set_property("weight", Pango.Weight.BOLD)
            column.set_min_width(min_width)
            column.set_resizable(True)
            self.tunnel_view.append_column(column)

        renderer = Gtk.CellRendererText()
        renderer.set_property("weight", Pango.Weight.BOLD)
        column = Gtk.TreeViewColumn("Status", renderer, text=4, foreground=5)
        column.set_min_width(90)
        self.tunnel_view.append_column(column)

        renderer = Gtk.CellRendererText()
        renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
        renderer.set_property("foreground", COLOR_DIM)
        column = Gtk.TreeViewColumn("Messages", renderer, text=6)
        column.set_min_width(200)
        column.set_expand(True)
        column.set_resizable(True)
        self.tunnel_view.append_column(column)
        self.tunnel_view.set_tooltip_column(6)

        scrolled.add(self.tunnel_view)
        vbox.pack_start(scrolled, True, True, 0)

        # Status bar
        self.statusbar = Gtk.Statusbar()
        vbox.pack_start(self.statusbar, False, False, 0)

        # Load saved tunnels
        self.refresh_tunnel_list()

        # Update status periodically, scan for outside tunnels a bit less often
        GLib.timeout_add_seconds(2, self.update_status)
        GLib.timeout_add_seconds(5, self.auto_scan)
        # One scan at startup; auto_scan returns True to keep the timer, so wrap it
        GLib.idle_add(lambda: self.auto_scan() and False)

        # Handle window close to hide instead of quit (when running with indicator)
        self.connect("delete-event", self.on_window_delete)

    def _install_manage_tunnel_css(self):
        """Dark theme plus toolbar button styling."""
        settings = Gtk.Settings.get_default()
        if settings is not None:
            settings.set_property("gtk-application-prefer-dark-theme", True)
        css = b"""
        @define-color bg_canvas #111217;
        @define-color bg_primary #181b1f;
        @define-color bg_secondary #22252b;
        @define-color border_weak #2c3235;
        @define-color text_primary #ccccdc;
        @define-color text_dim #8e8e9a;
        @define-color accent #ff7f00;
        @define-color selected_bg #2c3442;

        window, dialog, messagedialog, .background {
            background-color: @bg_primary;
            color: @text_primary;
        }

        treeview.view, textview, textview text {
            background-color: @bg_canvas;
            color: @text_primary;
        }

        treeview.view:selected, treeview.view:selected:focus {
            background-color: @selected_bg;
            color: #ffffff;
        }

        treeview.view header button {
            background: @bg_secondary;
            color: @text_dim;
            border-color: @border_weak;
            box-shadow: none;
            text-shadow: none;
        }

        entry {
            background: @bg_canvas;
            color: @text_primary;
            border-color: @border_weak;
        }

        entry:focus {
            border-color: @accent;
            box-shadow: inset 0 0 0 1px @accent;
        }

        entry.error {
            border-color: #f2495c;
            color: #f2495c;
        }

        infobar box {
            background: #1f2a3a;
            color: @text_primary;
            border: none;
        }

        button.suggested-action {
            background: #2f7d32;
            border-color: #2a6b2d;
            color: #ffffff;
        }

        button.suggested-action:hover {
            background: #388e3c;
        }

        button.select-button {
            background: #16913a;
            border-color: #127a30;
            padding: 12px 24px;
            font-size: 17px;
            border-radius: 10px;
        }

        button.select-button label {
            font-size: 20px;
        }

        button.select-button:hover {
            background: #19a442;
        }

        .color-picker headerbar button {
            background: transparent;
            border-color: transparent;
            box-shadow: none;
        }

        .color-picker headerbar button:hover {
            background: @bg_secondary;
        }

        button.edit-toggle {
            padding: 0 8px;
            min-height: 0;
            font-size: 12px;
            color: @text_dim;
        }

        button.edit-toggle:checked {
            background: @bg_secondary;
            color: @text_primary;
        }

        entry.hex-entry {
            font-family: monospace;
            font-size: 15px;
            padding: 8px 10px;
            border-radius: 8px;
        }

        statusbar {
            background: @bg_secondary;
            color: @text_dim;
        }

        scrolledwindow {
            border: 1px solid @border_weak;
        }

        toolbar.manage-tunnel-toolbar {
            background: @bg_primary;
            border: none;
            border-radius: 0;
            padding: 0;
            box-shadow: none;
        }

        toolbar.manage-tunnel-toolbar toolbutton button {
            color: #e8e8e8;
            border-radius: 2px;
            border: 1px solid @border_weak;
            padding: 6px 14px;
            min-height: 34px;
            background-image: none;
            text-shadow: none;
            box-shadow: none;
            margin-right: 6px;
        }

        toolbar.manage-tunnel-toolbar toolbutton button image,
        toolbar.manage-tunnel-toolbar toolbutton button label {
            color: inherit;
        }

        toolbar.manage-tunnel-toolbar image.toolbar-button-icon {
            color: #f1f3f5;
            opacity: 0.92;
        }

        toolbar.manage-tunnel-toolbar separator {
            min-width: 8px;
            border: none;
            background: transparent;
        }

        toolbar.manage-tunnel-toolbar toolbutton.toolbar-button-neutral button {
            background: @bg_secondary;
        }

        toolbar.manage-tunnel-toolbar toolbutton.toolbar-button-neutral button:hover {
            background: #2c3039;
            border-color: #3d424d;
        }

        toolbar.manage-tunnel-toolbar toolbutton.toolbar-button-success button {
            background: #233127;
            border-color: #34503b;
        }

        toolbar.manage-tunnel-toolbar toolbutton.toolbar-button-success button:hover {
            background: #2b3d30;
            border-color: #73bf69;
        }

        toolbar.manage-tunnel-toolbar toolbutton.toolbar-button-danger button {
            background: #36222a;
            border-color: #57323b;
        }

        toolbar.manage-tunnel-toolbar toolbutton.toolbar-button-danger button:hover {
            background: #432830;
            border-color: #f2495c;
        }

        toolbar.manage-tunnel-toolbar toolbutton button:checked,
        toolbar.manage-tunnel-toolbar toolbutton button:active {
            background-image: none;
            box-shadow: inset 0 1px 2px alpha(black, 0.24);
        }

        toolbar.manage-tunnel-toolbar toolbutton:last-child button {
            margin-right: 0;
        }
        """
        provider = Gtk.CssProvider()
        try:
            provider.load_from_data(css)
        except GLib.Error as error:
            print(f"Could not load CSS: {error}")
            return

        screen = Gdk.Screen.get_default()
        if screen is not None:
            Gtk.StyleContext.add_provider_for_screen(
                screen,
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

    def _style_toolbar_button(self, button, variant_class):
        """Add button classes and force labels to stay visible."""
        button.set_is_important(True)
        button.get_style_context().add_class(variant_class)
        icon_widget = button.get_icon_widget()
        if icon_widget is not None:
            icon_widget.get_style_context().add_class("toolbar-button-icon")

    def _create_toolbar_icon(self, icon_name):
        """Create a consistently sized symbolic icon for toolbar buttons."""
        icon = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.MENU)
        icon.set_pixel_size(16)
        icon.get_style_context().add_class("toolbar-button-icon")
        return icon

    def on_window_delete(self, widget, event):
        """Handle window close - hide instead of quit when using indicator"""
        if self.app_indicator:
            self.hide()
            return True  # Prevent window destruction
        else:
            # Keep the window open when the user cancels quitting
            return not self.on_quit(widget)

    def _row_values(self, config):
        forwards = config.get('forwards', [])
        if forwards:
            # Show first local port + count
            local_port = f"{forwards[0].get('local_port', '-')} (+{len(forwards)-1})"
        else:
            local_port = config.get('local_port', '-')
        user = config.get('ssh_user')
        ssh_host = f"{user}@{config.get('ssh_host')}" if user else config.get('ssh_host', '')
        status, status_color, message = self.tunnel_manager.status(config)
        return [config.get('name', ''),
                config.get('type', 'local').capitalize(),
                ssh_host,
                local_port,
                status,
                status_color,
                message,
                config,
                status in ON_STATUSES,
                normalize_hex(config.get('color')) or COLOR_DIM]

    def refresh_tunnel_list(self):
        """Refresh the tunnel list view, keeping the selection"""
        selected = self._selected_config()
        selected_id = selected.get('id') if selected else None
        self.tunnel_store.clear()
        for config in self.tunnels_config:
            treeiter = self.tunnel_store.append(self._row_values(config))
            if selected_id and config.get('id') == selected_id:
                self.tunnel_view.get_selection().select_iter(treeiter)

    def update_status(self):
        """Update tunnel status and messages in the list"""
        for row in self.tunnel_store:
            status, status_color, message = self.tunnel_manager.status(row[7])
            switch_on = status in ON_STATUSES
            if (row[4], row[5], row[6], row[8]) != (status, status_color, message, switch_on):
                row[4], row[5], row[6], row[8] = status, status_color, message, switch_on
        return True

    def _selected_config(self):
        if not hasattr(self, 'tunnel_view'):
            return None
        model, treeiter = self.tunnel_view.get_selection().get_selected()
        return model[treeiter][7] if treeiter else None

    def _tunnels_changed(self):
        self.config_manager.save_tunnels(self.tunnels_config)
        if self.app_indicator:
            self.app_indicator.update_menu()
        else:
            self.refresh_tunnel_list()

    def window_select(self, tunnel_id):
        for row in self.tunnel_store:
            if row[7].get('id') == tunnel_id:
                self.tunnel_view.get_selection().select_iter(row.iter)

    def on_switch_toggled(self, renderer, path):
        """Switch in the list starts or stops that row's tunnel"""
        self.tunnel_view.get_selection().select_path(path)
        if self.tunnel_store[path][8]:
            self.on_stop_tunnel(None)
        else:
            self.on_start_tunnel(None)

    def on_row_activated(self, view, path, column):
        """Enter on a row opens Edit; mouse double-clicks are handled in on_list_button_press"""
        if column is not self.switch_column:
            self.tunnel_view.get_selection().select_path(path)
            self.on_edit_tunnel(None)

    EDIT_CLICK_WINDOW = 1.0

    def on_list_button_press(self, view, event):
        """Two clicks on the same row within a second open Edit. Only the switch toggles a tunnel."""
        if event.button != 1:
            return False
        if event.type != Gdk.EventType.BUTTON_PRESS:
            # GTK's own double-click would also activate the row; the timing below decides
            return True
        hit = view.get_path_at_pos(int(event.x), int(event.y))
        if not hit or hit[1] is self.switch_column:
            self._last_click = None
            return False
        tunnel_id = self.tunnel_store[hit[0]][7].get('id')
        now = time.monotonic()
        last = self._last_click
        if last and last[0] == tunnel_id and now - last[1] <= self.EDIT_CLICK_WINDOW:
            self._last_click = None
            view.get_selection().select_path(hit[0])
            GLib.idle_add(lambda: self.on_edit_tunnel(None) and False)
            return True
        self._last_click = (tunnel_id, now)
        return False

    def on_add_tunnel(self, widget, prefill=None, tint_row_hint=None):
        """Add a new tunnel configuration"""
        dialog = TunnelDialog(self, prefill, self.color_memory, tint_row_hint)
        if not prefill or not prefill.get('color'):
            dialog.set_color(ConfigManager.new_tunnel_fields(self.tunnels_config)['color'])
        if dialog.run_until_valid() == Gtk.ResponseType.OK:
            data = dialog.get_data()
            data['id'] = ConfigManager.new_tunnel_fields(self.tunnels_config)['id']
            self.color_memory.remember(data['color'], dialog.tint_row)
            self.tunnels_config.append(data)
            self._tunnels_changed()
            self.show_message("Tunnel configuration added")

        dialog.destroy()

    def _next_free_port(self, port):
        """port, or the first higher one not used by a tunnel or a listening socket"""
        try:
            candidate = int(port)
        except (TypeError, ValueError):
            return port
        used = {tunnel_port(c) for c in self.tunnels_config} | set(self.tunnel_manager.scanner.listeners())
        while str(candidate) in used and candidate < 65535:
            candidate += 1
        return str(candidate)

    def on_duplicate_tunnel(self, widget):
        """Open Add prefilled with the selected tunnel, a free port and a lighter or darker color"""
        config = self._selected_config()
        if not config:
            return
        copy = json.loads(json.dumps(config))
        copy.pop('id', None)
        copy['name'] = f"{config.get('name', '')} (copy)"
        if copy.get('type') != 'remote' and not copy.get('forwards'):
            copy['local_port'] = self._next_free_port(copy.get('local_port'))
        base = normalize_hex(config.get('color')) or TUNNEL_PALETTE[0]
        row = self.color_memory.row_for(base)
        # The neighbouring tint in the same row: darker for the light half, lighter for the dark half
        slot = row.index(base)
        copy['color'] = row[slot + 1] if slot < MIDDLE_TINT + 1 else row[slot - 1]
        self.on_add_tunnel(None, prefill=copy, tint_row_hint=row)

    def on_edit_tunnel(self, widget):
        """Edit selected tunnel; a running tunnel is restarted with the new settings"""
        config = self._selected_config()
        if not config:
            self.show_error("Please select a tunnel to edit")
            return

        dialog = TunnelDialog(self, config, self.color_memory)
        if dialog.run_until_valid() == Gtk.ResponseType.OK:
            new_data = dialog.get_data()
            self.color_memory.remember(new_data['color'], dialog.tint_row)
            tunnel_id = config.get('id')
            was_running = self.tunnel_manager.is_running(tunnel_id)
            if was_running:
                self.tunnel_manager.stop_tunnel(tunnel_id)
            for i, c in enumerate(self.tunnels_config):
                if c.get('id') == tunnel_id:
                    self.tunnels_config[i] = new_data
                    break
            self.tunnel_manager.scanner._stamp = 0.0
            if was_running:
                self.tunnel_manager.start_tunnel(tunnel_id, new_data)
            self._tunnels_changed()
            self.show_message("Tunnel configuration updated"
                              + (" and restarted" if was_running else ""))

        dialog.destroy()

    def on_remove_tunnel(self, widget):
        """Remove selected tunnel configuration"""
        config = self._selected_config()
        if not config:
            self.show_error("Please select a tunnel to remove")
            return
        tunnel_id = config.get('id')
        if self.tunnel_manager.is_running(tunnel_id):
            self.tunnel_manager.stop_tunnel(tunnel_id)
        self.tunnels_config = [c for c in self.tunnels_config if c.get('id') != tunnel_id]
        self._tunnels_changed()
        self.show_message(f"Tunnel '{config.get('name')}' removed")

    def on_start_tunnel(self, widget):
        """Start selected tunnel"""
        config = self._selected_config()
        if not config:
            self.show_error("Please select a tunnel to start")
            return
        success, message = self.tunnel_manager.start_tunnel(config.get('id'), config)
        if success:
            self.show_message(f"Tunnel '{config.get('name')}' started")
        else:
            self.show_message(f"Tunnel '{config.get('name')}' not started: {message}")
        self.update_status()
        if self.app_indicator:
            self.app_indicator.update_menu_status()

    def on_stop_tunnel(self, widget):
        """Stop selected tunnel, also when it was opened outside the app"""
        config = self._selected_config()
        if not config:
            self.show_error("Please select a tunnel to stop")
            return
        tunnel_id = config.get('id')
        if self.tunnel_manager.is_running(tunnel_id):
            self.tunnel_manager.stop_tunnel(tunnel_id)
            self.show_message(f"Tunnel '{config.get('name')}' stopped")
        elif self.tunnel_manager.status(config)[0] == "External":
            self.stop_external(config)
        else:
            self.tunnel_manager.stop_tunnel(tunnel_id)
            self.show_message(f"Tunnel '{config.get('name')}' is offline")
        self.tunnel_manager.scanner._stamp = 0.0
        self.update_status()
        if self.app_indicator:
            self.app_indicator.update_menu_status()

    def stop_external(self, config):
        """Ask, then close a tunnel port that an ssh process outside the app holds"""
        port = tunnel_port(config)
        owner = self.tunnel_manager.port_owner(port) or {}
        command = ' '.join(PortScanner.cmdline(owner.get('pid', 0)))
        dialog = Gtk.MessageDialog(
            parent=self, flags=0, message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=f"Port {port} was opened outside this app. Stop it?")
        dialog.format_secondary_text(
            f"pid {owner.get('pid')}: {command}\n\n"
            "If this is an interactive ssh session, stopping it closes that session too. "
            "A shared ControlMaster connection only gets this port cancelled.")
        response = dialog.run()
        dialog.destroy()
        if response != Gtk.ResponseType.YES:
            return
        success, message = self.tunnel_manager.stop_external(config)
        if success:
            self.show_message(message)
        else:
            self.show_error(message)

    def on_start_all(self, widget):
        started = 0
        for config in self.tunnels_config:
            tunnel_id = config.get('id')
            if self.tunnel_manager.status(config)[0] in ON_STATUSES:
                continue
            if self.tunnel_manager.start_tunnel(tunnel_id, config)[0]:
                started += 1
        self.show_message(f"Started {started} tunnel(s)")
        self.update_status()
        if self.app_indicator:
            self.app_indicator.update_menu_status()

    def on_stop_all(self, widget):
        for config in self.tunnels_config:
            self.tunnel_manager.stop_tunnel(config.get('id'))
        self.tunnel_manager.scanner._stamp = 0.0
        self.show_message("Stopped all tunnels started by this app")
        self.update_status()
        if self.app_indicator:
            self.app_indicator.update_menu_status()

    def _new_external(self):
        """Outside tunnels that are not in the list and not dismissed"""
        known_ports = {tunnel_port(c) for c in self.tunnels_config}
        return [e for e in self.tunnel_manager.scanner.external_tunnels(self.tunnel_manager.own_pids())
                if e['port'] not in known_ports and (e['port'], e['pid']) not in self.ignored_external]

    def auto_scan(self):
        found = self._new_external()
        if found:
            ports = ", ".join(f"{e['port']} ({e['host']})" for e in found)
            self.scan_label.set_text(f"Found {len(found)} tunnel(s) opened outside the app: {ports}")
            self.scan_bar.show()
        else:
            self.scan_bar.hide()
        return True

    def on_scan_bar_response(self, bar, response):
        if response == Gtk.ResponseType.OK:
            self.on_scan(None)
        else:
            self.ignored_external.update((e['port'], e['pid']) for e in self._new_external())
        self.auto_scan()

    def on_selection_changed(self, selection):
        selected = self._selected_config()
        for name in ("Terminal", "Duplicate"):
            self.toolbar_buttons[name].set_sensitive(selected is not None)

    # Choices in the dialog for Terminal on a tunnel that is not open
    TERMINAL_SESSION, TERMINAL_TUNNEL, TERMINAL_BOTH = 1, 2, 3

    def on_open_terminal(self, widget):
        """Open an ssh session in the terminal; for a closed tunnel, ask what to open"""
        config = self._selected_config()
        if not config:
            return
        if self.tunnel_manager.status(config)[0] not in ON_STATUSES:
            choice = self.ask_terminal_choice(config)
            if choice in (self.TERMINAL_TUNNEL, self.TERMINAL_BOTH):
                self.on_start_tunnel(None)
            if choice not in (self.TERMINAL_SESSION, self.TERMINAL_BOTH):
                return
        self.open_ssh_session(config)

    def ask_terminal_choice(self, config):
        """Ask whether to start the tunnel, open an ssh session, or both"""
        host = config.get('ssh_host', '')
        user = config.get('ssh_user')
        dest = f"{user}@{host}" if user else host
        port = tunnel_port(config)
        dialog = Gtk.MessageDialog(
            parent=self, flags=0, message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text=f"Tunnel '{config.get('name', '')}' is not open")
        dialog.format_secondary_text(
            f"Start tunnel: opens local port {port} through {dest}, no shell.\n"
            f"Open SSH session: runs ssh {dest} in the terminal, a shell on the remote host. "
            "It does not open the tunnel's port.")
        dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                           "Open SSH session", self.TERMINAL_SESSION,
                           "Start tunnel", self.TERMINAL_TUNNEL,
                           "Both", self.TERMINAL_BOTH)
        dialog.set_default_response(self.TERMINAL_TUNNEL)
        response = dialog.run()
        dialog.destroy()
        return response

    def open_ssh_session(self, config):
        """Run an interactive ssh to the tunnel's host in the default terminal"""
        cmd = ['ssh']
        if config.get('ssh_port'):
            cmd += ['-p', str(config['ssh_port'])]
        user = config.get('ssh_user')
        cmd.append(f"{user}@{config['ssh_host']}" if user else config['ssh_host'])
        try:
            launcher = subprocess.Popen(terminal_command() + cmd, start_new_session=True,
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
            # Reap the launcher so it does not linger as a zombie
            threading.Thread(target=launcher.wait, daemon=True).start()
            self.show_message(f"Opened {' '.join(cmd)} in terminal")
        except OSError as e:
            self.show_error(f"Could not open terminal: {e}")

    def on_scan(self, widget):
        """List tunnels opened outside this app and offer to add them"""
        found = self.tunnel_manager.scanner.external_tunnels(self.tunnel_manager.own_pids())
        known_ports = {tunnel_port(c) for c in self.tunnels_config}

        dialog = Gtk.Dialog(title="Tunnels opened outside the app", parent=self, flags=0)
        dialog.add_buttons(Gtk.STOCK_CLOSE, Gtk.ResponseType.CLOSE,
                           "Add selected", Gtk.ResponseType.OK)
        dialog.set_default_size(820, 300)
        box = dialog.get_content_area()
        box.set_spacing(6)
        for margin in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{margin}")(12)

        if not found:
            box.pack_start(Gtk.Label(label="No ssh tunnels found outside this app.", xalign=0),
                           False, False, 0)
            dialog.set_response_sensitive(Gtk.ResponseType.OK, False)
        # add, port, type, host, pid, command, in_list, entry
        store = Gtk.ListStore(bool, str, str, str, str, str, bool, object)
        for entry in found:
            in_list = entry['port'] in known_ports
            host = f"{entry['user']}@{entry['host']}" if entry['user'] else entry['host']
            store.append([not in_list, entry['port'], entry['type'], host, str(entry['pid']),
                          entry['command'] + ("   (already in list)" if in_list else ""),
                          not in_list, entry])
        view = Gtk.TreeView(model=store)
        view.get_style_context().add_class("tunnel-list")
        toggle = Gtk.CellRendererToggle()

        def on_toggled(renderer, path):
            if store[path][6]:
                store[path][0] = not store[path][0]
        toggle.connect("toggled", on_toggled)
        view.append_column(Gtk.TreeViewColumn("Add", toggle, active=0, activatable=6))
        for title, index in (("Port", 1), ("Type", 2), ("Host", 3), ("PID", 4)):
            view.append_column(Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=index))
        renderer = Gtk.CellRendererText()
        renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
        renderer.set_property("foreground", COLOR_DIM)
        column = Gtk.TreeViewColumn("Command", renderer, text=5)
        column.set_expand(True)
        view.append_column(column)
        view.set_tooltip_column(5)
        scrolled = Gtk.ScrolledWindow()
        scrolled.add(view)
        box.pack_start(scrolled, True, True, 0)
        dialog.show_all()

        added = 0
        if dialog.run() == Gtk.ResponseType.OK:
            for row in store:
                if not (row[0] and row[6]):
                    continue
                entry = row[7]
                config = {
                    'name': f"{entry['host']} :{entry['port']}",
                    'type': entry['type'],
                    'ssh_user': entry['user'],
                    'ssh_host': entry['host'],
                    'ssh_port': entry['ssh_port'],
                    'local_port': entry['port'],
                    'remote_host': entry['remote_host'],
                    'remote_port': entry['remote_port'],
                }
                config.update(ConfigManager.new_tunnel_fields(self.tunnels_config))
                if entry['type'] == 'local' and not entry['remote_host']:
                    self.tunnel_manager.messages[config['id']] = \
                        "remote host/port unknown (shared ssh connection), set them via Edit"
                self.tunnels_config.append(config)
                added += 1
        dialog.destroy()
        if added:
            self._tunnels_changed()
            self.show_message(f"Added {added} tunnel(s); they show as External until stopped")

    def on_import_command(self, widget):
        """Import SSH commands (supports multiple commands, one per line)"""
        dialog = Gtk.Dialog(
            title="Import SSH Commands",
            parent=self,
            flags=0
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OK, Gtk.ResponseType.OK
        )
        dialog.set_default_size(600, 400)

        box = dialog.get_content_area()
        box.set_spacing(6)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)

        label = Gtk.Label(label="Paste SSH commands (supports multiline with \\ or indentation):")
        label.set_xalign(0)
        box.pack_start(label, False, False, 0)

        # Text view for multi-line input
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)

        text_view = Gtk.TextView()
        text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        scrolled.add(text_view)
        box.pack_start(scrolled, True, True, 0)

        # Example text
        example_label = Gtk.Label()
        example_label.set_markup("<small><i>Examples:\n# Single tunnel\nssh -L 8080:localhost:80 user@host\n# Multiple forwards\nssh -L 27017:mongo-0:27017 \\\n    -L 27018:mongo-1:27017 -p 2222 user@host</i></small>")
        example_label.set_xalign(0)
        box.pack_start(example_label, False, False, 0)

        dialog.show_all()
        response = dialog.run()

        if response == Gtk.ResponseType.OK:
            text_buffer = text_view.get_buffer()
            start_iter = text_buffer.get_start_iter()
            end_iter = text_buffer.get_end_iter()
            input_text = text_buffer.get_text(start_iter, end_iter, False).strip()

            if input_text:
                # Pre-process to handle multiline commands with backslash continuation
                # Replace backslash-newline with space
                processed_text = input_text.replace('\\\n', ' ')

                # Split by lines and process each command
                lines = processed_text.split('\n')
                imported_count = 0
                failed_count = 0
                error_messages = []
                existing_names = [t.get('name') for t in self.tunnels_config]

                # Track the last comment to use as tunnel name
                pending_name = None

                # Track lines that are part of a multiline command (without backslash)
                current_command = []
                command_start_line = 0

                for line_num, line in enumerate(lines, 1):
                    line = line.strip()

                    # Skip empty lines
                    if not line:
                        continue

                    # Check if this is a comment line
                    if line.startswith('#'):
                        # If we have a pending multiline command, process it first
                        if current_command:
                            full_command = ' '.join(current_command)
                            try:
                                config = SSHCommandParser.parse_ssh_command(full_command)
                                if pending_name:
                                    config['name'] = pending_name
                                if config['name'] in existing_names:
                                    counter = 1
                                    base_name = config['name']
                                    while f"{base_name}_{counter}" in existing_names:
                                        counter += 1
                                    config['name'] = f"{base_name}_{counter}"
                                existing_names.append(config['name'])
                                self.tunnels_config.append(config)
                                imported_count += 1
                            except Exception as e:
                                failed_count += 1
                                error_messages.append(f"Line {command_start_line}: {str(e)}")
                            current_command = []
                            pending_name = None

                        # Extract the name from the comment (remove # and whitespace)
                        comment_text = line[1:].strip()
                        if comment_text:
                            pending_name = comment_text
                        continue

                    # Check if this is the start of a new SSH command
                    if line.startswith('ssh'):
                        # If we have a pending multiline command, process it first
                        if current_command:
                            full_command = ' '.join(current_command)
                            try:
                                config = SSHCommandParser.parse_ssh_command(full_command)
                                if pending_name:
                                    config['name'] = pending_name
                                if config['name'] in existing_names:
                                    counter = 1
                                    base_name = config['name']
                                    while f"{base_name}_{counter}" in existing_names:
                                        counter += 1
                                    config['name'] = f"{base_name}_{counter}"
                                existing_names.append(config['name'])
                                self.tunnels_config.append(config)
                                imported_count += 1
                            except Exception as e:
                                failed_count += 1
                                error_messages.append(f"Line {command_start_line}: {str(e)}")
                            pending_name = None

                        # Start new command
                        current_command = [line]
                        command_start_line = line_num
                    elif current_command:
                        # This is a continuation line (doesn't start with ssh)
                        current_command.append(line)
                    else:
                        # Orphaned line that doesn't start with ssh and no current command
                        failed_count += 1
                        error_messages.append(f"Line {line_num}: Command must start with 'ssh'")

                # Process the last command if any
                if current_command:
                    full_command = ' '.join(current_command)
                    try:
                        # Parse the SSH command
                        config = SSHCommandParser.parse_ssh_command(full_command)

                        # Use the pending name from comment if available
                        if pending_name:
                            config['name'] = pending_name
                            pending_name = None  # Reset for next command

                        # Check if tunnel with this name already exists
                        if config['name'] in existing_names:
                            # Make the name unique
                            counter = 1
                            base_name = config['name']
                            while f"{base_name}_{counter}" in existing_names:
                                counter += 1
                            config['name'] = f"{base_name}_{counter}"

                        # Add to existing names to avoid duplicates in the same import
                        existing_names.append(config['name'])

                        # Add the tunnel
                        self.tunnels_config.append(config)
                        imported_count += 1

                    except ValueError as e:
                        failed_count += 1
                        error_messages.append(f"Line {line_num}: {str(e)}")
                        pending_name = None  # Reset on error
                    except Exception as e:
                        failed_count += 1
                        error_messages.append(f"Line {line_num}: {str(e)}")
                        pending_name = None  # Reset on error

                # Save if any tunnels were imported
                if imported_count > 0:
                    # Saving and reloading gives the imported tunnels an id and color
                    self.config_manager.save_tunnels(self.tunnels_config)
                    self.tunnels_config = self.config_manager.load_tunnels()
                    self._tunnels_changed()

                # Show results
                if imported_count > 0 and failed_count == 0:
                    self.show_message(f"Successfully imported {imported_count} tunnel(s)")
                elif imported_count > 0 and failed_count > 0:
                    result_msg = f"Imported {imported_count} tunnel(s), {failed_count} failed:\n\n" + "\n".join(error_messages[:5])
                    if len(error_messages) > 5:
                        result_msg += f"\n... and {len(error_messages) - 5} more errors"
                    dialog_result = Gtk.MessageDialog(
                        parent=self,
                        flags=0,
                        message_type=Gtk.MessageType.WARNING,
                        buttons=Gtk.ButtonsType.OK,
                        text="Partial Import"
                    )
                    dialog_result.format_secondary_text(result_msg)
                    dialog_result.run()
                    dialog_result.destroy()
                elif failed_count > 0:
                    error_msg = "Failed to import commands:\n\n" + "\n".join(error_messages[:5])
                    if len(error_messages) > 5:
                        error_msg += f"\n... and {len(error_messages) - 5} more errors"
                    self.show_error(error_msg)
                else:
                    self.show_error("No valid SSH commands found (empty lines and comments are ignored)")
            else:
                self.show_error("Please enter at least one SSH command")

        dialog.destroy()

    def on_export_commands(self, widget):
        """Export all tunnel configurations as SSH commands"""
        if not self.tunnels_config:
            self.show_error("No tunnels to export")
            return

        # Generate SSH commands
        commands = []
        for config in self.tunnels_config:
            try:
                cmd = SSHCommandParser.export_to_command(config)
                commands.append(f"# {config.get('name')}\n{cmd}\n")
            except Exception as e:
                print(f"Error exporting {config.get('name')}: {e}")

        if not commands:
            self.show_error("No valid tunnels to export")
            return

        export_text = "\n".join(commands)

        # Show export dialog
        dialog = Gtk.Dialog(
            title="Export SSH Commands",
            parent=self,
            flags=0
        )
        dialog.add_buttons(
            Gtk.STOCK_CLOSE, Gtk.ResponseType.CLOSE
        )
        dialog.set_default_size(600, 400)

        box = dialog.get_content_area()
        box.set_spacing(6)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)

        label = Gtk.Label(label="SSH commands for all tunnels:")
        label.set_xalign(0)
        box.pack_start(label, False, False, 0)

        # Text view to display commands
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)

        text_view = Gtk.TextView()
        text_view.set_editable(False)
        text_view.set_cursor_visible(False)
        text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        text_buffer = text_view.get_buffer()
        text_buffer.set_text(export_text)
        scrolled.add(text_view)
        box.pack_start(scrolled, True, True, 0)

        # Copy to clipboard button
        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        copy_button = Gtk.Button.new_with_label("Copy to Clipboard")
        copy_button.connect("clicked", self.on_copy_to_clipboard, text_buffer)
        button_box.pack_start(copy_button, False, False, 0)
        box.pack_start(button_box, False, False, 0)

        dialog.show_all()
        dialog.run()
        dialog.destroy()

    def on_copy_to_clipboard(self, widget, text_buffer):
        """Copy text buffer contents to clipboard"""
        start_iter = text_buffer.get_start_iter()
        end_iter = text_buffer.get_end_iter()
        text = text_buffer.get_text(start_iter, end_iter, False)

        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clipboard.set_text(text, -1)
        self.show_message("Copied to clipboard")

    def show_message(self, message):
        """Show a message in the statusbar"""
        context_id = self.statusbar.get_context_id("main")
        self.statusbar.pop(context_id)
        self.statusbar.push(context_id, message)

    def show_error(self, message):
        """Show an error dialog"""
        dialog = Gtk.MessageDialog(
            parent=self,
            flags=0,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK,
            text=message
        )
        dialog.run()
        dialog.destroy()

    def confirm_quit(self):
        """Ask before quitting while tunnels run, since quitting stops them"""
        running = [c.get('name', '') for c in self.tunnels_config
                   if self.tunnel_manager.is_running(c.get('id'))]
        if not running:
            return True
        dialog = Gtk.MessageDialog(
            parent=self if self.get_visible() else None, flags=0,
            message_type=Gtk.MessageType.WARNING, buttons=Gtk.ButtonsType.NONE,
            text=f"Quit and stop {len(running)} running tunnel(s)?")
        dialog.format_secondary_text(
            "Quitting closes every tunnel started by this app:\n" + "\n".join(running))
        dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                           "Quit and stop tunnels", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.CANCEL)
        dialog.set_keep_above(True)
        response = dialog.run()
        dialog.destroy()
        return response == Gtk.ResponseType.OK

    def on_quit(self, widget):
        """Stop all tunnels and quit, after confirmation; returns False when cancelled"""
        if not self.confirm_quit():
            return False
        self.tunnel_manager.cleanup()
        Gtk.main_quit()
        return True


class SSHTunnelIndicator:
    """System tray indicator for SSH tunnels"""

    def __init__(self):
        self.tunnel_manager = SSHTunnelManager()
        self.config_manager = ConfigManager()
        self.tunnels_config = self.config_manager.load_tunnels()

        # Setup custom icon theme path
        # Try local directory first, then system installation directory
        script_dir = os.path.dirname(os.path.abspath(__file__))
        local_icons = os.path.join(script_dir, "icons")
        system_icons = "/usr/local/share/easier-ssh-tunnel/icons"

        if os.path.exists(local_icons):
            self.icon_theme_path = local_icons
        elif os.path.exists(system_icons):
            self.icon_theme_path = system_icons
        else:
            self.icon_theme_path = local_icons  # Fallback to local

        # Icon names (without extension)
        self.icon_name_white = "easy-ssh-tunnel-white"
        self.icon_name_green = "easy-ssh-tunnel-green"

        # Create the indicator with icon theme path
        self.indicator = AppIndicator3.Indicator.new(
            APP_ID,
            self.icon_name_white,
            AppIndicator3.IndicatorCategory.APPLICATION_STATUS
        )
        self.indicator.set_icon_theme_path(self.icon_theme_path)
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_title(APP_NAME)

        # Set attention icon for when tunnels are active (green)
        self.indicator.set_attention_icon(self.icon_name_green)

        # Store current state
        self.currently_active = False

        # Create main window (hidden initially) and pass shared managers
        self.window = EasySSHTunnelApp(
            app_indicator=self,
            tunnel_manager=self.tunnel_manager,
            config_manager=self.config_manager
        )

        # Build the menu
        self.menu = Gtk.Menu()
        self.build_menu()
        self.indicator.set_menu(self.menu)

        # Update menu and icon periodically to refresh status
        GLib.timeout_add_seconds(2, self.update_menu_status)

    STATUS_EMOJI = {"Running": "🟢", "Connecting": "🟡", "External": "🟠",
                    "Stopped": "🔴", "Offline": "⚪"}

    def _menu_label(self, config):
        """(status, label); the label starts with the status emoji"""
        status = self.tunnel_manager.status(config)[0]
        port = tunnel_port(config)
        label = f"{self.STATUS_EMOJI.get(status, '⚪')}  {config.get('name', 'Unknown')}"
        return status, (f"{label}  :{port}" if port else label)

    def build_menu(self):
        """Build the indicator menu"""
        # Clear existing menu items
        for item in self.menu.get_children():
            self.menu.remove(item)

        # Store references to tunnel menu items for status updates
        self.tunnel_menu_items = {}

        if self.tunnels_config:
            for config in self.tunnels_config:
                _, label_text = self._menu_label(config)
                # GNOME Shell draws the image on the right: the tunnel's exact color
                menu_item = Gtk.ImageMenuItem(label=label_text)
                menu_item.set_image(Gtk.Image.new_from_pixbuf(color_dot_pixbuf(config.get('color'))))
                menu_item.set_always_show_image(True)
                menu_item.connect("activate", self.toggle_tunnel, config)
                menu_item.show_all()
                self.menu.append(menu_item)
                self.tunnel_menu_items[config.get('id')] = (menu_item, config)
        else:
            item = Gtk.MenuItem(label="No tunnels configured")
            item.set_sensitive(False)
            item.show()
            self.menu.append(item)

        separator = Gtk.SeparatorMenuItem()
        separator.show()
        self.menu.append(separator)

        settings_item = Gtk.MenuItem(label="Manage Tunnels...")
        settings_item.connect("activate", self.show_main_window)
        settings_item.show()
        self.menu.append(settings_item)

        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", self.quit_app)
        quit_item.show()
        self.menu.append(quit_item)

    def update_menu(self):
        """Rebuild the menu (called when tunnels are added/removed/edited)"""
        self.tunnels_config = self.config_manager.load_tunnels()
        self.window.tunnels_config = self.tunnels_config
        self.window.refresh_tunnel_list()
        self.build_menu()

    def update_menu_status(self):
        """Update menu status indicators periodically without rebuilding menu"""
        any_running = False
        for menu_item, config in getattr(self, 'tunnel_menu_items', {}).values():
            status, label_text = self._menu_label(config)
            if status in ON_STATUSES:
                any_running = True
            if menu_item.get_label() != label_text:
                menu_item.set_label(label_text)

        # Switch between ACTIVE (normal icon) and ATTENTION (active icon)
        if any_running and not self.currently_active:
            self.indicator.set_status(AppIndicator3.IndicatorStatus.ATTENTION)
            self.currently_active = True
        elif not any_running and self.currently_active:
            self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
            self.currently_active = False

        return True

    def toggle_tunnel(self, widget, config):
        """Toggle tunnel on/off; tunnels opened outside the app are handled in the window"""
        tunnel_id = config.get('id')
        status = self.tunnel_manager.status(config)[0]
        if self.tunnel_manager.is_running(tunnel_id):
            self.tunnel_manager.stop_tunnel(tunnel_id)
        elif status == "External":
            self.show_main_window()
            self.window.window_select(tunnel_id)
            self.window.stop_external(config)
        else:
            self.tunnel_manager.start_tunnel(tunnel_id, config)
        self.tunnel_manager.scanner._stamp = 0.0
        self.window.update_status()
        self.update_menu_status()

    def show_main_window(self, widget=None):
        """Show the main configuration window"""
        self.window.show_all()
        self.window.present()
        # Request focus and move to current workspace
        self.window.present_with_time(Gdk.CURRENT_TIME)
        self.window.set_keep_above(False)  # Ensure it's a normal window

    def quit_app(self, widget):
        """Quit the application; asks first when tunnels are running"""
        self.window.on_quit(widget)


def main():
    import sys

    signal.signal(signal.SIGINT, signal.SIG_DFL)  # Allow Ctrl+C to quit

    use_indicator = '--no-indicator' not in sys.argv
    reason = ''
    if use_indicator and AppIndicator3 is None:
        use_indicator, reason = False, "no AppIndicator library installed"
    elif use_indicator and not tray_available():
        use_indicator, reason = False, "no system tray found"

    if use_indicator:
        try:
            # Run with system tray indicator
            indicator = SSHTunnelIndicator()
            Gtk.main()
            return
        except Exception as e:
            reason = f"tray failed: {e}"

    # Window mode: closing the window quits the app
    app = EasySSHTunnelApp()
    app.show_all()
    if reason:
        print(f"Running without tray icon: {reason}")
        app.show_message(f"Running without tray icon ({reason}). On GNOME, enable the "
                         "AppIndicator extension to get one; closing this window quits the app.")
    Gtk.main()


if __name__ == '__main__':
    main()
