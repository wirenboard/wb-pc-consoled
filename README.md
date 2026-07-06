# wb-pc-consoled — exclusive Wiren Board debug-console keeper

A small host-PC daemon that **owns** the USB debug console of a Wiren
Board controller. It runs on your Linux workstation / bench host (hence
`pc` — it is *not* installed on the controller), one instance per board.
Everything else — humans, scripts, AI agents — interacts only with the
daemon's files and FIFOs; nobody touches `/dev/ttyACMn` directly, ever.

Built for benches where several tools (and several AI agents) fight over
the same debug console. Solves three chronic problems:

1. **Console lands on a different `/dev/ttyACMn` after reconnect** — the
   daemon finds the port by stable serial:
   `/dev/serial/by-id/*<SERIAL>*-if00`, and transparently reopens after
   re-enumeration. Log files and FIFOs never move.
2. **CDC-ACM wedges until a USB reset** — `wb-pc-console reset <INST>`
   does a `USBDEVFS_RESET` ioctl on the parent USB device. Bursts of I/O
   errors while the device is still enumerated trigger an automatic
   reset; *silence* does not (a quiet console is normal), so deciding on
   a reset for a silent-but-suspicious console is left to the operator
   or agent.
3. **Third parties grab the port** — the daemon holds it with `TIOCEXCL`
   (other non-root `open()`s fail with EBUSY) and additionally sweeps
   `fuser` every second, SIGKILLing any process that got hold of the tty
   (killed intruders are logged in `ts.log` and counted in
   `status.json`).

## Install

```sh
git clone https://github.com/wirenboard/wb-pc-consoled
cd wb-pc-consoled
./install.sh SERIAL1 [SERIAL2 ...]     # instance = board serial from the by-id name
```

The instance name is the serial part of the by-id symlink, e.g. for
`/dev/serial/by-id/usb-Wiren_Board_Wiren_Board_7_Debug_Console_ABCD1234-if00`
use `ABCD1234`. `wb-pc-console list` shows what is attached.

The installer symlinks the two scripts into `~/.local/bin`, installs a
user systemd template unit `wb-pc-console@.service`, enables lingering
(so daemons survive logout), and starts the requested instances.

ModemManager runs as root, so the daemon cannot lock it out or kill it;
keep it away from the consoles with the shipped udev rule (one-time,
needs root):

```sh
sudo cp 99-wb-pc-console.rules /etc/udev/rules.d/ && sudo udevadm control --reload
```

Requirements: Linux, Python 3 (stdlib only), `fuser` (psmisc; a pure
`/proc` fallback is built in), systemd user session.

## Per-instance files (`~/wb-pc-console/<INST>/`)

| file          | meaning                                                        |
|---------------|----------------------------------------------------------------|
| `raw.log`     | every received byte, verbatim, no timestamps                   |
| `ts.log`      | one line per console line: `[2026-07-06T17:31:02.123456789+0300] text` (ns precision, stamped at first byte of the line); daemon events as `=== wb-pc-console: …`, markers as `==== MARK: …` |
| `in`          | FIFO → bytes written here go out the serial port               |
| `ctl`         | FIFO → commands: `reset` `break` `reopen` `mark <text>` `flush`|
| `status.json` | connected?, tty, rx/tx counters, kills, last reset, pid        |

Logs rotate at 512 MB (keep 3, `tail -F`-safe). Partial lines (prompts
like `login:`) are flushed into `ts.log` after 1 s of line silence.

## Usage cheatsheet

```sh
wb-pc-console list                                  # what's here, what's up
wb-pc-console send ABCD1234 "uname -a"              # keystrokes + Enter
wb-pc-console run  ABCD1234 "date" --expect '2026' --timeout 10   # send + wait, rc=0 on match
wb-pc-console expect ABCD1234 'login:' --timeout 120             # just wait for pattern
wb-pc-console key  ABCD1234 ctrl-c enter            # named keys; also sysrq-<x> (break+key)
wb-pc-console reset ABCD1234                        # USB reset (the "unstick" hammer)
wb-pc-console mark ABCD1234 "suspend test #5"       # marker line into ts.log
wb-pc-console tail ABCD1234 --ts                    # follow timestamped log
wb-pc-console status ABCD1234 ; wb-pc-console files ABCD1234
wb-pc-console attach ABCD1234                       # interactive (humans), exit Ctrl-]
```

`run`/`expect` follow `ts.log` from the pre-send offset — no tty
contention, no lost races. Regexes match against the whole log line
(timestamp prefix included).

Sending raw bytes without the helper also works — the FIFO is the API:
`printf 'root\n' > ~/wb-pc-console/ABCD1234/in`.

## Why agents can't stop it (and how a human can)

- `RefuseManualStop=yes` — `systemctl --user stop/restart` is refused.
- `Restart=always` + `RestartSec=1` + `StartLimitIntervalSec=0` — any
  kill (including SIGKILL, including `fuser -k` from a rogue agent)
  revives it within ~1 s; input FIFOs and logs survive the bounce.
- `loginctl enable-linger` — survives logout/no session.
- **Upgrade** (after updating the scripts): `systemctl --user kill wb-pc-console@INST`
  — it comes back with the new code.
- **Genuine stop** (humans only): drop the `RefuseManualStop=yes` line in
  `~/.config/systemd/user/wb-pc-console@.service`, `systemctl --user daemon-reload`,
  then `systemctl --user stop wb-pc-console@INST`.

## Environment knobs

Set via `systemctl --user edit wb-pc-console@INST` (see the unit file for
the list): `WB_PC_CONSOLE_BAUD` (115200), `WB_PC_CONSOLE_GLOB` (device
glob override), `WB_PC_CONSOLE_KILL_INTERVAL` (1 s sweep),
`WB_PC_CONSOLE_WRITE_DELAY_MS` (per-byte TX pacing for paste-sensitive
bootloaders), `WB_PC_CONSOLE_MAX_BYTES` / `WB_PC_CONSOLE_ROTATE_KEEP`
(rotation), `WB_PC_CONSOLE_AUTO_RESET_IDLE` (opt-in idle reset, off by
default), `WB_PC_CONSOLE_RESET_COOLDOWN` (30 s), `WB_PC_CONSOLE_LOG_TX`
(log sent chunks into ts.log, on).

## Mechanism notes

- Exclusivity is `ioctl(TIOCEXCL)`: kernel-level, applies to any later
  non-root `open()`. The 1 s `fuser` sweep catches whoever opened the
  port *before* the daemon (or root processes — those are logged but
  can't be killed from a user daemon).
- USB reset resolves `tty → /sys/class/tty/<t>/device → parent USB
  device → busnum/devnum → ioctl(USBDEVFS_RESET)` on
  `/dev/bus/usb/BBB/DDD`. On most distros the usbfs node is writable by
  the console owner or via ACLs; if not, the daemon falls back to
  passwordless `sudo`, and failing that logs an actionable error. After
  the reset the device re-enumerates (possibly as a new ttyACMn) and the
  daemon reattaches via the by-id glob.
- The port is opened with `HUPCL` cleared, so daemon restarts don't
  glitch DTR/RTS toward the board.
- WB7/WB8 debug consoles are Holtek `04d9:b534`; both expose `-if00` as
  the console interface. Other serials work too — override
  `WB_PC_CONSOLE_GLOB` for non-Wiren-Board adapters.
