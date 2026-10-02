# Easier SSH Tunnel Manager

> Fork of [ProjectMakersDE/EasySSHTunnelManager](https://github.com/ProjectMakersDE/EasySSHTunnelManager) with the changes from upstream PRs #2 to #6 merged:
>
> | Change | Upstream PR |
> |---|---|
> | Tunnels keyed by id, ports closed on stop with ControlMaster in ~/.ssh/config, optional user/port, Ayatana tray support, English labels | [#2](https://github.com/ProjectMakersDE/EasySSHTunnelManager/pull/2) |
> | Running / Connecting / Stopped / Offline status, ssh errors in a Messages column, ON/OFF switch per row, Start all / Stop all, open in terminal, quit confirmation | [#3](https://github.com/ProjectMakersDE/EasySSHTunnelManager/pull/3) |
> | Detection of tunnels opened outside the app | [#4](https://github.com/ProjectMakersDE/EasySSHTunnelManager/pull/4) |
> | Tunnel colors and a dark theme | [#5](https://github.com/ProjectMakersDE/EasySSHTunnelManager/pull/5) |
> | Autostart question in install.sh | [#6](https://github.com/ProjectMakersDE/EasySSHTunnelManager/pull/6) |
>
> The app is renamed to Easier SSH Tunnel Manager: command `easier_ssh_tunnel.py`, config in `~/.config/easier-ssh-tunnel/`. On first start it copies the tunnels from `~/.config/easy-ssh-tunnel/`, so it can be installed next to the original.
>
> The screenshots below use demo tunnels on example.com hosts.

![Logo](icons/logo.png)

**A simple, user-friendly GUI application for managing SSH tunnels on Linux.** Easier SSH Tunnel Manager lets you create, configure, and monitor local, remote, and dynamic SSH tunnels through an intuitive interface with system tray integration.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.6+](https://img.shields.io/badge/python-3.6+-blue.svg)](https://www.python.org/downloads/)
[![GTK 3](https://img.shields.io/badge/GTK-3.0-green.svg)](https://www.gtk.org/)

---

## Screenshots

### Main Window
![Main Window](screenshots/main-window.png)

*Main window: ON/OFF switch, tunnel color, status and the last ssh error per tunnel. The bar above the list shows a tunnel found outside the app.*

### System Tray Integration
![System Tray](screenshots/system-tray.png)

*Tray menu: status on the left (🟢 running, 🟡 connecting, 🔴 stopped, 🟠 external, ⚪ offline), tunnel color on the right. Click a tunnel to switch it on or off.*

### Add/Edit Tunnel Dialog
![Add Tunnel](screenshots/add-tunnel.png)

*Tunnel dialog with color picker. User and Port left empty are taken from `~/.ssh/config`.*

---

## Features

- **System Tray Integration:**
  - Icon appears in the top bar (system tray)
  - Left-click: Quick menu showing all tunnels with status indicators
  - Click on tunnel name to toggle it on/off
  - Each tunnel shows its status (🟢 running, 🟡 connecting, 🔴 stopped, ⚪ offline) and local port
  - "Manage Tunnels..." option to open the full configuration GUI

- **Multiple Tunnel Types:**
  - Local port forwarding (`-L`): Forward a local port to a remote destination
  - Remote port forwarding (`-R`): Forward a remote port to a local destination
  - Dynamic port forwarding (`-D`): Create a SOCKS proxy

- **Easy Management:**
  - Add, edit, and remove tunnel configurations
  - Start and stop tunnels with the ON/OFF switch on each row, by double-clicking a row, or with the toolbar buttons
  - Start all or stop all tunnels at once
  - View tunnel status in real-time, with the last ssh error (for example a port that is already in use) in the Messages column
  - Open an interactive ssh session to a tunnel's host in the default terminal
  - Detect tunnels opened outside the app (a plain `ssh -D/-L` in a terminal, or a forward on a shared ControlMaster connection) and add them to the list
  - Persistent configuration storage

- **User-Friendly Interface:**
  - Dark GTK3 interface
  - A color per tunnel, picked or entered as hex, shown in the list and as a dot in the tray menu
  - Simple dialog for configuring tunnels
  - Real-time status indicators

## Requirements

- Python 3.6+ with PyGObject and pycairo
- GTK 3 GObject introspection
- AyatanaAppIndicator3 or AppIndicator3 introspection, for the tray icon (optional: without it the app runs as a window)
- OpenSSH client
- `ss` from iproute2, for status and port detection

## Installation

### Install with install.sh (recommended)

```bash
git clone https://github.com/solidsalad/EasierSSHTunnelManager.git
cd EasierSSHTunnelManager
sudo ./install.sh
```

install.sh detects the package manager (apt, dnf, pacman or zypper) and:
- installs the dependencies listed below for your distribution
- copies the app to `/usr/local/bin/easier_ssh_tunnel.py` and the icons to `/usr/local/share/easier-ssh-tunnel/`
- adds "Easier SSH Tunnel Manager" to the applications menu
- on GNOME without tray support, installs the AppIndicator extension where the distribution packages it and prints how to enable it
- asks whether to start the app at login

### Dependencies per distribution

| Distribution | Command |
|---|---|
| Ubuntu 22.04+, Debian 12+, Linux Mint, Pop!_OS | `sudo apt-get install python3 python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 iproute2 openssh-client` |
| Fedora (RHEL, Rocky, Alma: enable EPEL first) | `sudo dnf install python3 python3-gobject python3-cairo gobject-introspection gtk3 libayatana-appindicator-gtk3 iproute openssh-clients` |
| Arch Linux, Manjaro, EndeavourOS | `sudo pacman -S --needed python python-gobject python-cairo gtk3 libayatana-appindicator iproute2 openssh` |
| openSUSE Tumbleweed, Leap | `sudo zypper install python3 python3-gobject python3-gobject-cairo python3-gobject-Gdk typelib-1_0-Gtk-3_0 typelib-1_0-AyatanaAppIndicator3-0_1 iproute2 openssh-clients` |

On older Debian and Ubuntu releases without the Ayatana package, use `gir1.2-appindicator3-0.1` instead.

install.sh was tested on 2026-10-02 in containers of Ubuntu 24.04, Debian 12, Fedora (latest), Arch Linux (latest) and openSUSE Tumbleweed: dependencies install, the app starts under a virtual display, falls back to window mode without a tray, and reports ssh errors. The tray icon itself was tested on Ubuntu 24.04 GNOME.

### Tray icon per desktop

| Desktop | Tray icon |
|---|---|
| KDE Plasma, Cinnamon, Xfce 4.16+ | built in |
| GNOME on Ubuntu | works out of the box (Ubuntu AppIndicators extension) |
| GNOME on Fedora, Debian, Arch, openSUSE | needs the "AppIndicator and KStatusNotifierItem Support" extension, see below |

Enable the GNOME extension after installing it (install.sh installs the package on Fedora, Debian and Arch; on openSUSE get it from [extensions.gnome.org](https://extensions.gnome.org/extension/615/appindicator-support/)), then log out and back in:

```bash
gnome-extensions enable appindicatorsupport@rgcjonas.gmail.com
```

When no tray is available the app opens its window instead of hiding in the tray, and closing the window quits it.

### Run without installing

After installing the dependencies for your distribution:

```bash
cd EasierSSHTunnelManager
./easier_ssh_tunnel.py
```

### Updating to Latest Version

```bash
cd EasierSSHTunnelManager
git pull
sudo ./install.sh
```

Your tunnel configurations in `~/.config/easier-ssh-tunnel/tunnels.json` are kept.

## Usage

### Running the Application

**With System Tray (default):**
```bash
./easier_ssh_tunnel.py
```

The application will start minimized to the system tray. Look for the network icon in the top bar.

**Without System Tray (window only):**
```bash
./easier_ssh_tunnel.py --no-indicator
```

### Using the System Tray

1. **Click the icon** in the top bar to open the quick menu
2. You'll see all configured tunnels with status indicators:
   - 🟢 = running, the local port is open
   - 🟡 = connecting, ssh is running but the port is not open yet
   - 🟠 = external, the port is held by an ssh process outside the app
   - 🔴 = stopped, the tunnel was switched on but the connection went down
   - ⚪ = offline, the tunnel is switched off
3. **Click on a tunnel name** to toggle it on/off
4. **Click "Manage Tunnels..."** to open the full configuration window
5. **Click "Quit"** to exit the application

### Managing Tunnels

1. Click **"Manage Tunnels..."** from the tray menu or run with `--no-indicator`
2. Use the toolbar buttons:
   - **Add** - Create a new tunnel configuration
   - **Edit** - Modify an existing tunnel
   - **Remove** - Delete a tunnel configuration
   - **Start all** / **Stop all** - Start every tunnel that is not open, or stop every tunnel started by the app
   - **Start** - Activate a tunnel
   - **Stop** - Deactivate a tunnel; on a stopped tunnel this clears it to offline
   - **Terminal** - Open `ssh [user@]host` in the default terminal (`x-terminal-emulator`). Greyed out until a tunnel is selected. When the tunnel is not open, a dialog offers **Start tunnel**, **Open SSH session** or **Both**; the ssh session alone does not open the tunnel's port.
3. The switch at the start of each row shows whether the tunnel's port is open and follows the connection: it turns off when ssh exits.
4. **Quit** (tray menu, or closing the window with `--no-indicator`) stops every tunnel the app started. When tunnels are running, the app asks first.

### Tunnel Status

| Status | Meaning |
|---|---|
| Running | ssh runs and the local port is open |
| Connecting | ssh runs, the local port is not open yet |
| Stopped | the tunnel was switched on, but ssh exited; the Messages column shows why |
| External | the tunnel's port is held by an ssh process outside the app |
| Offline | the tunnel is switched off |

### Tunnels Opened Outside the App

Every 5 seconds the app looks for local ports held by `ssh` processes it did not start. New ones appear in a bar above the list: **Review** opens a dialog to add them, **Ignore** hides them. A tunnel in the list whose port is held this way shows as **External**. Stopping it asks first, then ends that ssh process, or for a forward on a shared ControlMaster connection runs `ssh -O cancel`.

### Adding a Tunnel

1. Click the **Add** button in the toolbar
2. Fill in the tunnel configuration:
   - **Tunnel Name**: A descriptive name for this tunnel
   - **Color**: Pick a color or type a hex value like `#7eb26d`
   - **Tunnel Type**: Choose Local, Remote, or Dynamic
   - **SSH Connection**: User, host, and port for the SSH server. Leave User and Port empty to take them from `~/.ssh/config`; a `Host` alias from that file works as host.
   - **Tunnel Details**: Port forwarding configuration
3. Click **OK** to save

### Tunnel Types Explained

#### Local Port Forwarding (-L)
Forward a local port to a remote destination through the SSH server.

Example: Access a database on a remote network
- Local Port: 3306
- Remote Host: database.internal.example.com
- Remote Port: 3306

After starting, connect to `localhost:3306` to access the remote database.

#### Remote Port Forwarding (-R)
Forward a remote port on the SSH server to a local destination.

Example: Share a local web server with others through the SSH server
- Local Port: 8080
- Remote Host: localhost
- Remote Port: 9090

After starting, others can access your local web server at `ssh-server:9090`.

#### Dynamic Port Forwarding (-D)
Create a SOCKS proxy for dynamic traffic routing.

Example: Route browser traffic through the SSH server
- SOCKS Port: 1080

Configure your browser to use `localhost:1080` as a SOCKS5 proxy.

## Configuration

Tunnel configurations are stored in:
```
~/.config/easier-ssh-tunnel/tunnels.json
```

You can manually edit this file if needed, but it's recommended to use the GUI.

## SSH Key Authentication

This application uses the system SSH client and your `~/.ssh/config`, so it supports all SSH authentication methods configured on your system:
- Password authentication (will prompt when starting tunnel)
- SSH key authentication (recommended)

For passwordless operation, set up SSH key authentication:

```bash
# Generate SSH key if you don't have one
ssh-keygen -t ed25519

# Copy your public key to the remote server
ssh-copy-id user@remote-server
```

Each tunnel opens its own SSH connection (`-o ControlMaster=no -o ControlPath=none`). With connection sharing enabled in `~/.ssh/config`, a forward would otherwise be added to an already open master connection and stay open after the tunnel is stopped. `ExitOnForwardFailure=yes` makes a tunnel exit when its port cannot be bound, so it does not show as running without a working forward.

## Autostart on Login

`sudo ./install.sh` asks whether to start the application at login and, when you answer yes, installs `~/.config/autostart/easier-ssh-tunnel.desktop` for the user who ran sudo. Tunnels are not started automatically.

To set it up by hand instead:

1. Open "Startup Applications" in Gnome
2. Click "Add"
3. Fill in:
   - **Name**: Easier SSH Tunnel Manager
   - **Command**: `/usr/local/bin/easier_ssh_tunnel.py` (or full path to the script)
   - **Comment**: Manage SSH tunnels from system tray
4. Click "Add"

The application will now start in the system tray on login.

## Troubleshooting

### Tunnel won't start
- Check that you can SSH to the server manually: `ssh user@host`
- Verify the ports are not already in use; the Messages column names the process that holds the port
- Check SSH server configuration allows port forwarding

### Permission denied
- Ensure you have SSH access to the remote server
- Set up SSH key authentication to avoid password prompts

### Tunnel shows as "Running" but doesn't work
- Check if the SSH process is actually running: `ps aux | grep ssh`
- Verify firewall settings on both local and remote systems
- Check SSH server logs for errors

### System tray icon doesn't appear
- On GNOME outside Ubuntu, install and enable the AppIndicator extension, see [Tray icon per desktop](#tray-icon-per-desktop)
- Check that the indicator library is installed: `gir1.2-ayatanaappindicator3-0.1` (Debian/Ubuntu), `libayatana-appindicator-gtk3` (Fedora), `libayatana-appindicator` (Arch), `typelib-1_0-AyatanaAppIndicator3-0_1` (openSUSE)
- Without a tray the app opens its window; the status bar says why

### Terminal button opens the wrong terminal
The app uses `$TERMINAL` when set, then `x-terminal-emulator` (Debian/Ubuntu) or `xdg-terminal-exec`, then the desktop's own terminal (Ptyxis, GNOME Console, GNOME Terminal, Konsole, Xfce Terminal, MATE Terminal, ...). Set `TERMINAL` in your session to pick another one.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request. For major changes, please open an issue first to discuss what you would like to change.

### How to Contribute

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Support

If you find this project useful, please consider:
- Giving it a star on GitHub
- Reporting bugs and suggesting features via [GitHub Issues](../../issues)
- Contributing code or documentation improvements


## Acknowledgments

- Built with [GTK3](https://www.gtk.org/) and [PyGObject](https://pygobject.readthedocs.io/)
- System tray integration powered by [AppIndicator3](https://lazka.github.io/pgi-docs/#AppIndicator3-0.1)
- Inspired by the need for a simple, user-friendly SSH tunnel manager on Linux
