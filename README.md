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
2. **The console goes mute until a USB reset** — two different faults:
   - The Holtek bridge of the WB7/WB8 debug console latches up when the
     host does not fetch its data in time: output stops, input still
     works, no error is reported. The daemon prevents the usual cause
     (nobody reading) by always reading, and cures the rest without a
     reset by re-sending the line coding — see "Holtek latch-up and the
     line-coding kick" below.
   - For anything else there is `wb-pc-console reset <INST>`: a
     `USBDEVFS_RESET` ioctl on the parent USB device, outcome in the exit
     code (0 = reset done). Bursts of I/O errors while the device is still
     enumerated trigger that reset automatically; *silence* never does (a
     quiet console is normal) — see "Idle watchdog" below.
3. **Third parties grab the port** — the daemon holds it with `TIOCEXCL`
   (other non-root `open()`s fail with EBUSY) and additionally sweeps
   `fuser` every second, SIGKILLing any process that got hold of the tty.
   Each PID is re-verified against `/proc/<pid>/fd` immediately before
   the kill, so a recycled PID is never killed by mistake. Killed
   intruders are logged in `ts.log` and counted in `status.json`.

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
needs root — the `trigger` applies it to already-plugged consoles):

```sh
sudo cp 99-wb-pc-console.rules /etc/udev/rules.d/ \
    && sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=tty
```

Requirements: Linux, Python 3 (stdlib only), `fuser` (psmisc; a pure
`/proc` fallback is built in), systemd user session.

## Per-instance files (`~/wb-pc-console/<INST>/`)

| file          | meaning                                                        |
|---------------|----------------------------------------------------------------|
| `raw.log`     | every received byte, verbatim, no timestamps                   |
| `ts.log`      | one line per console line: `[2026-07-06T17:31:02.123456789+0300] text` (ns precision, stamped at first byte of the line); daemon events as `=== wb-pc-console: …`, markers as `==== MARK: …` |
| `in`          | FIFO → bytes written here go out the serial port (queued up to 256 KiB while the port is down) |
| `ctl`         | FIFO → commands: `reset` `break` `reopen` `kick` `mark <text>` `flush`|
| `status.json` | connected?, tty, rx/tx counters, drops, kills, last reset, line kicks, pid |

The instance directory is `0700` and every file in it `0600`: the FIFOs
drive what is usually a **root shell** on the board, and with
`WB_PC_CONSOLE_LOG_TX=1` (the default) `ts.log` records everything you
*send* — including input the console does not echo, such as passwords
typed through `attach`. Set `WB_PC_CONSOLE_LOG_TX=0` if that is a concern.

Logs rotate at 512 MB (keep 3, `tail -F`-safe; rotation is crash-safe —
if it fails, capture continues into the old file). Partial lines (prompts
like `login:`) are flushed into `ts.log` after 1 s of line silence.

## Usage cheatsheet

```sh
wb-pc-console list                                  # what's here, what's up
wb-pc-console send ABCD1234 "uname -a"              # keystrokes + Enter (-n: no Enter)
wb-pc-console run  ABCD1234 "date" --expect '2026' --timeout 10   # send + wait, rc=0 on match
wb-pc-console expect ABCD1234 'login:' --timeout 120             # just wait for pattern
wb-pc-console key  ABCD1234 ctrl-c enter            # named keys; also sysrq-<x> (break+key)
wb-pc-console reset ABCD1234                        # USB reset; rc=0 only if it succeeded
wb-pc-console kick ABCD1234                         # mute Holtek console: re-send line coding
wb-pc-console mark ABCD1234 "suspend test #5"       # marker line into ts.log
wb-pc-console tail ABCD1234 --ts                    # follow timestamped log
wb-pc-console status ABCD1234 ; wb-pc-console files ABCD1234
wb-pc-console attach ABCD1234                       # interactive (humans), exit Ctrl-]
```

`run`/`expect` follow `ts.log` from the pre-send offset — no tty
contention, no lost races. Regexes match against the whole log line
(timestamp prefix included), but the daemon's own event lines
(`=== wb-pc-console: …`, including the TX echo of what you just sent)
are **skipped**, so expecting a token that appears in the sent command
cannot false-match. `reset` waits for the daemon's outcome event
(race-free: the log offset is captured before the request) and exits 0
only on "usb reset done"; `--no-wait` restores fire-and-forget.

Sending raw bytes without the helper also works — the FIFO is the API:
`printf 'root\n' > ~/wb-pc-console/ABCD1234/in`.

## Restart behavior (and how to stop it)

- `Restart=always` + `RestartSec=1` + `StartLimitIntervalSec=0` — any
  kill (including SIGKILL, including `fuser -k` from a rogue agent)
  revives the daemon within ~1 s; FIFOs and logs survive the bounce.
- `loginctl enable-linger` — survives logout/no session.
- **Upgrade** (after updating the scripts): `systemctl --user kill wb-pc-console@INST`
  — it comes back with the new code. A plain `systemctl --user restart`
  works too.
- **Stop**: `systemctl --user stop wb-pc-console@INST` (add `disable` to
  keep it from coming back on login).
- A daemon bug can not take the capture down either: the main loop
  catches everything, log-file write failures are counted in
  `status.json` (`log_dropped`) instead of crashing, and the port stays
  open and drained throughout.

## Holtek latch-up and the line-coding kick

The WB7/WB8 debug console bridge (Holtek HT42B534, `04d9:b534`) has a
128-byte UART→USB FIFO and no flow control. When the **130th** byte
arrives that the host has not fetched, the UART→USB direction stops
**for good**. That happens when

- the board prints ≥ 130 bytes while no program has the port open (a
  boot log, a getty banner), or
- the reader falls behind by ~4.1 KiB (the host side buffers about 4 KiB
  before the FIFO starts to fill) — 0.36 s of full-rate output at 115200.

Nothing is reported: no I/O error, no hangup, no notification, and
host→board keeps working, so the console looks alive but mute. Waiting,
close/reopen, DTR/RTS, break and `tcsetattr` with unchanged settings do
not help. What does: any `SET_LINE_CODING` request — and the Linux
cdc-acm driver sends one only when the settings *change*. A terminal
program that opens the port at the 115200 8N1 the previous one left
behind therefore sees nothing; so did this daemon after a restart.

The daemon forces the request out by setting `CSTOPB` and clearing it
again (8N1 → 8N2 → 8N1, two control requests, ~15 ms, baud untouched, no
USB reset, the tty stays open):

- right after every open of the port;
- when its own main loop was late by more than 3500 character times
  (0.3 s at 115200: laptop suspend, SIGSTOP, a blocked log write, an
  overloaded host) — logged in `ts.log` as `line coding re-sent: main
  loop stalled for … s`. Output of that interval is lost either way (the
  chip dropped it); the kick only ends the dead time;
- after `WB_PC_CONSOLE_IDLE_KICK` seconds (default 10) without a
  received byte (and 1 s after the last sent one), and again every such
  period while the silence lasts — so a mute console comes back within
  ~10 s even while someone keeps pressing Enter.
  For this chip silence does not prove health — a latched-up bridge and
  a quiet board look the same — so this one is not logged, only counted
  (`line_kicks` in `status.json`);
- on `wb-pc-console kick INST` (`kick` in the ctl FIFO).

Cost of a kick: the bridge drops what it holds at that instant. On a
silent line that is nothing, and the board sees no glitch. If the board
is printing right then, about 100 bytes of output are lost (measured at
full rate, 115200), and so is host→board data still inside the bridge —
which is why kicks while the port is open, manual ones included, wait
until the daemon's and kernel's TX queues are empty, then allow the
bridge to drain for at least 0.1 s (longer at low baud rates). This
interval starts when the empty queue is observed, even after a delayed
poll. The initial kick on opening the port clears stale bridge data
before the daemon sends anything.

Automatic kicks are sent only to `04d9:b534`
(`WB_PC_CONSOLE_LINE_KICK=auto`); `1` enables them for any adapter, `0`
turns them off. `WB_PC_CONSOLE_IDLE_KICK=0` keeps the open and stall
kicks but stops the periodic one. `status.json` shows
`line_kick_active`, `line_kicks` and `stall_kicks`.

`break` does nothing on this chip (it answers SEND_BREAK with a STALL),
so it is no way out of a latch-up either.

## Idle watchdog (optional, external)

The daemon deliberately has no "reset when silent" logic — silence is
normal for an idle console, and the one known silent failure (the Holtek
latch-up above) is handled without a reset. If you want one for a board that is supposed
to be chatty, poll `status.json` from outside:

```sh
age=$(python3 -c "import json;print(json.load(open('$HOME/wb-pc-console/ABCD1234/status.json'))['last_rx_age_s'])")
# if $age is too big for your taste:
wb-pc-console reset ABCD1234
```

## USB reset permissions

The reset needs write access to `/dev/bus/usb/BBB/DDD`. On many desktop
distros the node is already writable for the seated user; if the daemon
logs `usb reset FAILED: no write access…`, install a udev rule scoped to
the Wiren Board console (recommended — survives replug, no root daemon
involvement):

```
# /etc/udev/rules.d/99-wb-usbfs.rules
SUBSYSTEM=="usb", ATTR{idVendor}=="04d9", ATTR{idProduct}=="b534", MODE="0660", GROUP="dialout"
```

then `sudo udevadm control --reload && sudo udevadm trigger` and replug
once. The daemon also tries `sudo -n <this script> --usb-reset <node>`
as a fallback; the helper refuses paths outside `/dev/bus/usb/`. **Do
not** point a `NOPASSWD` sudoers rule at this script unless you accept
that anyone able to edit the script (it is user-writable) effectively
gets root — the udev rule is the safer standing fix.

## Environment knobs

Set via `systemctl --user edit wb-pc-console@INST`; the authoritative
list with defaults is in `wb-pc-consoled --help`. Summary:
`WB_PC_CONSOLE_BASE_DIR` (~/wb-pc-console), `WB_PC_CONSOLE_GLOB` (device
glob override), `WB_PC_CONSOLE_BAUD` (115200),
`WB_PC_CONSOLE_KILL_INTERVAL` (1 s sweep), `WB_PC_CONSOLE_WRITE_DELAY_MS`
(per-byte TX pacing for paste-sensitive bootloaders),
`WB_PC_CONSOLE_FLUSH_TIMEOUT` (1 s partial-line flush),
`WB_PC_CONSOLE_MAX_BYTES` / `WB_PC_CONSOLE_ROTATE_KEEP` (rotation),
`WB_PC_CONSOLE_LINE_KICK` (`auto`: line-coding kicks on the Holtek
bridge only; `1` any adapter; `0` off), `WB_PC_CONSOLE_IDLE_KICK` (10 s
of silence before a periodic kick; 0 = none),
`WB_PC_CONSOLE_RESET_COOLDOWN` (30 s — throttles **automatic** resets
only; manual `ctl` resets always fire), `WB_PC_CONSOLE_LOG_TX` (log sent
chunks into ts.log; **records non-echoed input, e.g. passwords**).

## Tests

`python3 -m unittest discover -s tests` — pure-logic units (line
assembly and ns stamping, log follower rotation handling, escape/key
tables, fuser output parsing, rotation, ctl parsing, status contract, line-coding kick conditions)
run against temp files; no hardware needed. CI runs them on every push.

## Mechanism notes

- Exclusivity is `ioctl(TIOCEXCL)`: kernel-level, applies to any later
  non-root `open()`. The 1 s `fuser` sweep catches whoever opened the
  port *before* the daemon (or root processes — those are logged but
  can't be killed from a user daemon). `fuser` is given 0.5 s; a slow or
  stuck `fuser` skips that round rather than stall the RX loop.
- USB reset resolves `tty → /sys/class/tty/<t>/device → parent USB
  device → busnum/devnum → ioctl(USBDEVFS_RESET)` on
  `/dev/bus/usb/BBB/DDD`. The target is always re-resolved from the
  live port or a fresh by-id lookup — never from a remembered
  `/dev/ttyACMn` path, which after renumbering may belong to a different
  device. After the reset the device re-enumerates (possibly as a new
  ttyACMn) and the daemon reattaches via the by-id glob.
- The port is opened with `HUPCL` cleared, so daemon restarts don't
  glitch DTR/RTS toward the board.
- WB7/WB8 debug consoles are Holtek `04d9:b534`; both expose `-if00` as
  the console interface. Other serials work too — override
  `WB_PC_CONSOLE_GLOB` for non-Wiren-Board adapters.
