# NMEA Web Forwarder

A centralized NMEA 0183 UDP forwarder with a built-in web dashboard.

Receives NMEA sentences over UDP, forwards them to configurable destinations
(unicast or broadcast), writes CSV logs, and exposes a REST API for monitoring
and management.

---

## Architecture

```
                    ┌──────────┐
                    │  Source  │  (serial-to-IP gateway)
                    │ NMEA 0183│
                    └────┬─────┘
                         │ UDP
    ┌────────────────────┼──────────────────────┐
    │                    ▼                       │
    │            ┌───────────────┐               │
    │            │  nmea-web     │               │
    │            │  :8080        │               │
    │            └───┬───────┬───┘               │
    │   Dashboard ▲  │       │  Forwarding        │
    │   (browser) │  │       ▼                   │
    │               │  ┌───────────┐             │
    │               │  │Destination│  (broadcast │
    │    /var/nmea/ │  │ UDP       │   or unicast)│
    │    *.csv  ◄───┘  └───────────┘             │
    └─────────────────────────────────────────────┘
```

The service is stateless at the protocol layer: it receives a UDP datagram,
writes it to a CSV log, and sends it unchanged to every configured destination.
No buffering, no reassembly, no validation — the consumer is responsible for
handling any inconsistencies.

## Prerequisites

| Requirement | Minimum |
|---|---|
| OS | Linux with systemd |
| Python | 3.9+ |
| Python packages | fastapi≥0.100, uvicorn[standard], jinja2, uvloop |
| Disk | ~140 MB/day per log file (typical NMEA traffic) |

The installer handles system dependencies automatically via pip.

## Quick Start

```bash
tar xzf nmea-web-v0.6-release.tar.gz
cd nmea-web-v0.6-release
sudo ./install.sh
```

Open `http://<server-ip>:8080` in a browser. Default credentials are `admin` /
`admin`.

## What the Installer Does

1. Creates the system user `nmea-web` (no login shell).
2. Creates `/opt/nmea-web/` (application) and `/var/nmea/` (log output).
3. Copies the application files and installs Python dependencies.
4. Installs the systemd unit and logrotate configuration.
5. Enables and starts the service.

The installer is idempotent — you can run it again to upgrade in place without
losing existing configuration or log data.

## Service Account and Security

The service runs as the **unprivileged** system user `nmea-web`. The systemd unit
applies additional hardening:

| Option | Effect |
|---|---|
| `NoNewPrivileges=yes` | Prevents privilege escalation via setuid/setgid |
| `PrivateTmp=yes` | Isolated `/tmp` directory |
| `ProtectHome=yes` | Blocks access to `/home` |
| `ProtectSystem=full` | `/usr`, `/boot`, `/etc` are read-only |
| `ReadWritePaths` | Only `/opt/nmea-web` and `/var/nmea` are writable |
| `AmbientCapabilities` | Grants `CAP_NET_BIND_SERVICE` and `CAP_NET_RAW` |

Only two Linux capabilities are needed:
- **CAP_NET_BIND_SERVICE** — bind UDP ports (needed for ports < 1024).
- **CAP_NET_RAW** — use `SO_BINDTODEVICE` to bind a socket to a specific
  network interface.

Root is never used.

## Configuration

All configuration is done through the web dashboard (or the REST API).

### Sources

A **source** defines a UDP listener that accepts incoming NMEA data.

| Field | Type | Description |
|---|---|---|
| `name` | string | Unique label (e.g. "depth", "seapath") |
| `listen_ip` | string | IP address to bind to. Default `0.0.0.0` (all interfaces) |
| `listen_port` | integer | UDP port to listen on |
| `interface` | string | *(optional)* Network device name (e.g. `ens36`). Binds the listener socket to a specific physical NIC via `SO_BINDTODEVICE`. Leave empty to listen on all interfaces |
| `enabled` | boolean | Whether the source starts automatically when the service boots |
| `log_enabled` | boolean | If checked, received data is written to a CSV log file |
| `log_file` | string | File path for the CSV log (e.g. `/var/nmea/depth_log.csv`) |

### Destinations

Each source can have one or more **destinations**. A destination defines where
the received UDP datagrams are forwarded.

| Field | Type | Description |
|---|---|---|
| `bind_ip` | string | *(optional)* IP address to bind the outbound socket to. Useful on multi-homed hosts when traffic must leave via a specific NIC |
| `dest_ip` | string | Target IP address |
| `dest_port` | integer | Target UDP port |
| `mode` | string | `unicast` (default) or `broadcast` |
| `prefix` | integer | *(broadcast only)* Subnet prefix length (1–31, default 24). The actual broadcast address is computed from `dest_ip` and this prefix |

### Unicast vs Broadcast

- **unicast**: packets are sent directly to `dest_ip:dest_port`. Use this when
  forwarding to a single known host.
- **broadcast**: the broadcast address is calculated as `dest_ip/<prefix>`
  (e.g. `10.23.10.255/24 → 10.23.10.255`). The socket is created with
  `SO_BROADCAST`. Use this when multiple consumers on the same subnet need the
  same data.

### Interface Binding (`bind_ip` and `interface`)

On a server with multiple NICs on different subnets, you may need to control
which interface is used:

- **Inbound (`interface`)**: forces the listener socket to only accept data on
  the specified device. Example: your NMEA sources arrive on `ens36`, but the
  server also has `ens33` for management.
- **Outbound (`bind_ip`)**: forces the outbound socket to source from the
  specified IP. Example: a destination expects packets from `10.23.10.222`, not
  from the server's default route.

A typical broadcast destination on a secondary interface:

```
bind_ip: 10.23.10.222
dest_ip: 10.23.10.255
prefix:  24
mode:    broadcast
```

This ensures the broadcast goes out via the `10.23.10.0/24` network rather than
the default route.

### CSV Log Format

Each received UDP datagram is written to the log file as one line:

```
<unix_timestamp>;<source_ip>;<nmea_sentence>
```

Example:

```
1782327892.197;192.168.4.21;$INHDT,120.8,T*2E
```

Empty datagrams (such as lone `\n` bytes from serial-to-IP gateways) are not
logged.

### Log Rotation

Log files are rotated daily via logrotate, keeping 14 compressed archives:

```
/var/nmea/*.csv {
    daily
    rotate 14
    missingok
    notifempty
    copytruncate
    compress
    delaycompress
    dateext
}
```

## REST API

All endpoints require HTTP Basic authentication (`admin` / `admin` by default).

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/api/sources` | List all sources with stats |
| `GET` | `/api/sources/{id}` | Get a single source |
| `POST` | `/api/sources` | Create a new source |
| `PUT` | `/api/sources/{id}` | Update a source |
| `DELETE` | `/api/sources/{id}` | Delete a source |
| `POST` | `/api/sources/{id}/start` | Start forwarding |
| `POST` | `/api/sources/{id}/stop` | Stop forwarding |
| `GET` | `/api/sources/{id}/stats` | Delta packet counters |
| `GET` | `/api/sources/{id}/log?lines=N` | Tail last N log lines |
| `GET` | `/api/interfaces` | List network interfaces and IPs |
| `GET` | `/api/system` | Memory, CPU, uptime, process stats |

### Stats

The stats endpoint returns delta counters since the last call:

```json
{
    "packets_in": 86,
    "packets_out": 86,
    "errors": 0,
    "dropped": 0,
    "running": true
}
```

| Field | Meaning |
|---|---|
| `packets_in` | Datagrams received since last poll |
| `packets_out` | Datagrams forwarded to at least one destination |
| `errors` | Failed `sendto()` calls (e.g. destination unreachable) |
| `dropped` | Reserved — always 0 in this release |
| `running` | Whether the source task is active |

## Files and Directories

| Path | Purpose |
|---|---|
| `/opt/nmea-web/` | Application code and SQLite database |
| `/var/nmea/` | CSV log files |
| `/etc/systemd/system/nmea-web.service` | Systemd unit |
| `/etc/logrotate.d/nmea-web` | Log rotation rules |

## Uninstall

```bash
sudo systemctl stop nmea-web
sudo systemctl disable nmea-web
sudo rm /etc/systemd/system/nmea-web.service
sudo rm /etc/logrotate.d/nmea-web
sudo systemctl daemon-reload
sudo rm -rf /opt/nmea-web
sudo userdel nmea-web 2>/dev/null || true
```

Log files under `/var/nmea/` are preserved — delete them manually if not needed.

---

**Author:** Rocco De Marco — CNR IRBIM

**License:** Apache License 2.0
