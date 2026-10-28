# Secure AceStream setup (example)

This document describes the optional hardened setup that the scripts in this repository implement. It is an
**example architecture**, not a product: read the scripts, adapt them to your system, and verify the result with the
leak tests below. Everything here is provided without warranty.

The goals:

- the AceStream engine may fetch streams from the internet,
- your real (home) IP address must not be visible to peers,
- the engine goes out **only through the VPN**; if the VPN is down it has **no fallback** over your normal connection,
- nobody on the LAN or the internet can reach the engine's ports,
- starting it is a single command.

Addresses in this document use documentation ranges (RFC 5737: `192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`).
Replace them with what your own system reports.

## Why this is needed

The AceStream engine is a P2P application. Without protection it

- connects to peers over whatever route the system picks, which exposes your real public IP, and
- listens on local ports (`6878`, `62062`, `8621`) on `0.0.0.0`, i.e. on all interfaces, including the LAN.

Check on your machine with `ss -tulpn | grep -E '6878|8621|62062'`.

Two rules follow from that:

1. **Block incoming access to the engine ports** (except from localhost).
2. **Restrict outgoing engine traffic hard to the VPN interface.**

## Architecture

```text
 Browser link (acestream://)          ace-play <ID>          ace-search (this app)
          |                                |                         |
          v                                |                         |
 acestream-launcher (URL handler)          |                         |
   `- engine-guard.sh                      |                         |
      (no engine -> message, abort)        |                         |
          |                                |                         |
          +----------------+---------------+-------------------------+
                           v
              player-ace.sh   (switch: mpv or VLC)
                |- mpv-ace.sh   (default)
                `- vlc-ace.sh   (alternative)
                           |   http://127.0.0.1:6878/ace/getstream?...
                           v
 acestream-start (keep the terminal open)
   `- engine runs as the dedicated user "acestreamvpn"
          |
          v
 ip rule prio 1000 (uidrange) -> table 100 -> default via <VPN gateway> dev tun0
          |       + nft table acestream_killswitch (only lo and the VPN interface, else drop)
          v
        tun0 -> VPN provider -> internet

 Incoming: nft table acestream_input_lock (ports 6878, 62062, 8621 only from localhost)
 Optional: host firewall (e.g. UFW) with default input DROP
```

### Protection layers applied by `acestream-start`

1. **Dedicated system user** (`acestreamvpn`, shell `nologin`). The script determines its UID with `id -u`, nothing
   is hard-coded.
2. **Policy routing:** `ip rule add uidrange <UID>-<UID> table 100 priority 1000`. Table 100 contains only
   `<tunnel network> dev tun0` and `default via <tunnel gateway> dev tun0`. The values are read from the running tunnel
   on every start, because gateway and network change with each server / reconnect.
3. **nftables kill switch** (`inet acestream_killswitch`, chain `output`, hook output, priority -100): for
   `meta skuid <UID>` only `oifname "lo"` and `oifname "<vpn if>"` are accepted, everything else is dropped.
4. **nftables input lock** (`inet acestream_input_lock`, chain `input`, hook input, priority -90, policy accept):
   for the ports 6878, 62062, 8621, TCP and UDP from `127.0.0.1` / `::1` are accepted, UDP replies
   (`ct state established,related`) are accepted, everything else on those ports is dropped.
5. **Abort conditions:** VPN not connected, no `tun[0-9]+` interface, or gateway / network not found.
6. **Leak test** (step 6 of the script, see below).
7. *Optional, outside of the scripts:* a provider-specific split-tunnel "VPN only" rule for the engine binaries, and a
   host firewall with default input DROP. The nftables rules are the hard guarantee; these are extra layers.

The script is **idempotent** (a second run replaces the rules without duplicates) and has **no rollback**: if it aborts,
existing rules stay in place (fail-closed).

### Why a separate user?

A dedicated user lets you write firewall and routing rules that apply to the engine and nothing else:

```text
your desktop user     -> normal networking, unchanged
acestreamvpn          -> may only leave through the VPN interface
```

Create it once (nobody logs in as this user):

```sh
sudo useradd -r -m -s /usr/bin/nologin acestreamvpn
```

## Prerequisites

- An **OpenVPN-style tunnel** that shows up as `tun0` (or another `tun[0-9]+`). The example uses Private Internet
  Access (PIA) set to OpenVPN, and `acestream-start` calls `piactl get connectionstate` / `piactl get vpnip`. For another
  VPN, replace step 1 (the connection check) and the optional `piactl get vpnip` comparison near the end; the rest only
  needs the `tun*` interface. WireGuard interfaces (`wg*`) are **not** detected by the script.
- Packages: `acestream-engine` (AUR), `nftables`, `iproute2`, `curl`, `sudo`, `mpv` (optional `vlc`),
  `acestream-launcher` (AUR) if you want `acestream://` links from the browser.
- The desktop user needs `sudo` (the script uses it for `ip`, `nft`, `pkill` and `sudo -u acestreamvpn`). If you
  restrict sudo with a sudoers drop-in, it must allow the exact commands the script runs, including the `curl`
  calls made as `acestreamvpn`.
- **Never enable the package's systemd unit** (`acestream-engine.service`). It runs the engine without any of the
  protection above. Keep it disabled.
- If your VPN client has a split-tunnel feature, note that it only affects the paths you list; the wrapper script,
  the symlink and the real binary are different paths.

## Files

| Path | Purpose | Owner |
|---|---|---|
| `/usr/local/bin/acestream-start` | start with all protection layers and leak test | root:root 755 |
| `/usr/local/bin/acestream-stop` | stop launcher, mpv and the engine | root:root 755 |
| `~/.config/acestream-launcher/config` | launcher config pointing at the guard and `player-ace.sh` | you |
| `~/.config/acestream-launcher/engine-guard.sh` | replaces the "engine" command: message and abort | you |
| `~/.config/acestream-launcher/player-ace.sh` | switches between mpv and VLC | you |
| `~/.config/acestream-launcher/mpv-ace.sh` | mpv wrapper with AceStream-specific options (default player) | you |
| `~/.config/acestream-launcher/vlc-ace.sh` | VLC wrapper with AceStream-specific options | you |
| `~/.config/acestream-launcher/ace-play` | play a stream by content ID against a running engine | you |
| `~/.config/acestream-launcher/player.choice` | optional: contains `mpv` or `vlc`, persistent choice | you |

Your normal `mpv.conf` and `vlcrc` are never modified; the wrappers override single options only for AceStream.
`./app/install.sh --with-launcher` installs the user-level files (it backs up existing ones).
The two root scripts are installed manually:

```sh
sudo install -m 755 -o root -g root scripts/acestream-start scripts/acestream-stop /usr/local/bin/
```

## Daily use

1. Connect the VPN (OpenVPN).
2. Run `acestream-start` in a terminal and **keep that terminal open**.
3. Open an `acestream://` link in the browser, run `ace-play <ID>`, or use the search app.
4. Done: close the player window, then run `acestream-stop`.

`acestream-stop` kills the launcher, **every** running `mpv` (`pkill -f mpv`) and the engine. It does **not**
remove nft tables, rules or routes; the protection stays in place. It does not close VLC; close that window yourself.

### `acestream-start` step by step

1. Check the VPN connection; find the tunnel interface, its address, network and gateway.
2. Kill old engine processes of the dedicated user.
3. Flush table 100 and rebuild it; remove old UID rules and add the new one.
4. Recreate the kill switch table.
5. Recreate the input lock table.
6. **Leak test:** `fetch_ip` asks `https://ifconfig.me` (fallback `https://api.ipify.org`) and accepts only a valid
   IPv4.
   - The engine user's public IP is fetched as `acestreamvpn`. If it is empty: ERROR and abort.
   - The host's own public IP is fetched as your normal user. If it is **equal** to the engine IP: "Leak" error and
     abort (exit 1). If it is empty: warning only, comparison skipped.
   - If the VPN client reports a different VPN IP than the engine user's public IP: warning only (the exit IP can
     differ from the server IP).
7. `exec sudo -u acestreamvpn -H /usr/bin/acestreamengine --client-console`; the terminal stays attached.

Expected output of step 6:

```text
AceStream-User öffentliche IP: 203.0.113.10        <- VPN exit address
Heim-IP (ohne VPN): 198.51.100.7                   <- your own address
OK: AceStream-IP weicht von der Heim-IP ab.
```

### `ace-play`

```sh
ace-play <40-hex-char content ID>
ace-play acestream://<ID>
```

It checks the engine at `http://127.0.0.1:6878/webui/api/service?method=get_version&format=json`; without an engine it
prints an error and exits 1 (invalid ID: exit 2). It never starts an engine itself.

### Launcher guard

Problem: the browser handler for `acestream://` starts `acestream-launcher %u`. If no engine answers on
`127.0.0.1:6878`, the launcher starts `acestreamengine --client-console` **as your desktop user**, i.e. without the
dedicated user, kill switch or policy routing. If your VPN client's own protection is off at that moment, the engine runs
with your real IP.

Solution: `~/.config/acestream-launcher/config` (the launcher uses `[tty]` when stdin is a terminal, otherwise `[browser]`)
replaces the engine command with `engine-guard.sh`:

```ini
[tty]
engine = /home/YOUR_USER/.config/acestream-launcher/engine-guard.sh
player = /home/YOUR_USER/.config/acestream-launcher/player-ace.sh
[browser]
engine = /home/YOUR_USER/.config/acestream-launcher/engine-guard.sh
player = /home/YOUR_USER/.config/acestream-launcher/player-ace.sh
```

- The launcher splits commands with `str.split()`, not `shlex`: **no quotes and no spaces in these paths.** It does not
  expand `~`, so the paths must be absolute (`install.sh --with-launcher` fills in your home).
- `engine-guard.sh` sends a critical desktop notification ("engine missing, run `acestream-start` first"), prints the
  same message on the launcher's terminal and terminates the launcher (`kill -TERM` on the parent, only if it is
  `acestream-launcher`), then exits 1. Why not simply `/usr/bin/false`: the launcher would wait for its 30 s timeout
  without any message.
- Limits: `acestream-launcher -e <cmd>` overrides the config, and starting `acestreamengine` by hand as your desktop
  user is still possible. **Don't.**

### Player chain

`player-ace.sh` picks the player: environment variable `ACE_PLAYER=mpv|vlc`, else the file `player.choice` (contents
`mpv` or `vlc`, also effective for browser starts where no environment is passed), else `mpv`.

`mpv-ace.sh` (used by default; also used directly for the official livestreams tab):

| Option | Effect |
|---|---|
| `--vf=bwdif=mode=send_field:parity=auto:deint=interlaced` | motion-adaptive deinterlacing at full field rate (50 fps for 1080i50); progressive material is not touched |
| `--deinterlace=no` | cancels `deinterlace=yes` from a user `mpv.conf` (otherwise filtered twice) |
| `--hwdec=no` | software decode |
| `--vo=gpu-next` | modern renderer (native Wayland) |
| `--video-sync=display-resample --interpolation --tscale=oversample` | even frame pacing when the stream rate does not divide the monitor refresh rate (e.g. 50 fps on 144 Hz) |
| `--glsl-shaders=` | clears a sharpening shader from your `mpv.conf` for streams (`ACE_SHARPEN=1` re-enables) |
| `--cache-pause*`, `--demuxer-max-*` | about 3 s start-up buffer, pause and re-buffer on underrun (`ACE_CACHE_S`) |
| `--network-timeout`, `--stream-lavf-o=reconnect...` | reconnect automatically |
| `--brightness/--gamma/--contrast/--saturation` | colour set explicitly for AceStream only (`ACE_BRIGHT`, `ACE_GAMMA`, `ACE_CONTRAST`, `ACE_SAT`, integers -100..100; invalid values fall back to defaults) |

`vlc-ace.sh` uses `--network-caching=3000 --http-reconnect --avcodec-hw=none --deinterlace=-1 --deinterlace-mode=yadif
--play-and-exit`. Reasons: software decoding avoids dropped pictures that VLC's VA-API path caused with 50 fps
deinterlacing in testing; `yadif` is calm but 25 fps; `linear` / `bob` give 50 fps but make static overlays (logos,
score tickers) jump per field. Switches: `ACE_CACHE=<ms>`, `ACE_DEINT=<mode>`, `ACE_HW=1`.

Both wrappers set the window / media title from `ACE_TITLE` when it is present (the search app sets it).

## Verifying that it is tight

With the engine running:

| Check | Expectation |
|---|---|
| `ps -o user,pid,cmd -C acestreamengine` | user `acestreamvpn` |
| `sudo ss -tunp \| grep -i ace` | only local addresses and the tunnel address; **your LAN / home address would be a leak** |
| `ip rule \| grep 1000` and `ip route show table 100` | a `uidrange` rule; table 100 with `default via <gateway> dev tun0` |
| `sudo nft list table inet acestream_killswitch` | rules present |
| `sudo nft list table inet acestream_input_lock` | rules present |
| `sudo -u acestreamvpn -H curl -4 ifconfig.me` | the VPN address, **not** your home address |
| `ip -s link show tun0` before / after starting a stream | counters increase |

Local reachability:

```sh
curl 'http://127.0.0.1:6878/webui/api/service?method=get_version'
```

expects a JSON answer with a `"version"` and `"error": null`.

LAN access must be blocked:

```sh
LANIP=$(ip -4 addr show scope global | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -1)
curl --connect-timeout 3 "http://$LANIP:6878/webui/api/service?method=get_version"
```

expects `curl: (28) Connection timed out`.

With the VPN switched off, an engine started by hand shows errors like `Temporary failure in name resolution` or
`sendto: Operation not permitted`. That is the desired behaviour: no VPN, no connectivity.

Fail-closed summary:

```text
VPN active and tun interface present -> engine starts
VPN not active                       -> acestream-start aborts / engine cannot connect
route missing                        -> engine cannot connect
wrong interface                      -> nftables drops
access from LAN / outside            -> blocked
```

## Troubleshooting

| Message / symptom | Cause | Fix |
|---|---|---|
| Desktop notification "AceStream: Engine fehlt" after clicking a link | no engine on 127.0.0.1:6878 | run `acestream-start`, keep the terminal, click the link again |
| `ace-play`: engine not running, exit 1 | engine not started | run `acestream-start` |
| `ace-play`: exit 2 | invalid content ID | use a 40-char hex ID or an `acestream://` link |
| "Leak: Engine-Verkehr verlässt das Haus ohne VPN" (exit 1) | engine IP equals host IP | do not use; check VPN state. Also a false alarm if the VPN client routes *all* traffic through the VPN (then both IPs are the VPN IP) |
| "AceStream-User bekommt keine öffentliche IP" | no route via the tunnel (reconnect, stale table 100) or sudoers too restrictive | rerun `acestream-start`; check sudoers for the engine user |
| abort in step 1 | VPN not connected | connect the VPN |
| abort: no `tun[0-9]+` / gateway / network missing | not an OpenVPN tunnel (e.g. WireGuard) | switch the VPN client to OpenVPN |
| no stream after reconnect / server change | table 100 points to the old gateway (fail-closed) | rerun `acestream-start` |
| VLC stays open after `acestream-stop` | the script only kills mpv | close the VLC window |
| picture too dark | gamma / brightness from your `mpv.conf` or the wrapper defaults | `ACE_GAMMA`, `ACE_BRIGHT`, ...; live keys in mpv: `1`/`2` contrast, `3`/`4` brightness, `5`/`6` gamma, `7`/`8` saturation |
| stutter, picture hangs then jumps | too few peers / too little throughput; or several players on one engine | other source for the same channel; `ACE_CACHE_S=10`; close extra players |
| too much delay | buffer too large | `ACE_CACHE_S=1` (mpv) / `ACE_CACHE=1500` (VLC) |
| audio problems | source audio codec | append `&transcode_audio=1` to the stream URL |

> **Do not call the engine's `command_url` with `method=stop` while someone is watching:** it ends the content for
> **all** clients of that engine. The search app never does this.

### Known harmless engine messages

```text
PermissionError: [Errno 13] Permission denied: '/usr/lib/acestream-engine/engine_runtime.json'
Failed to init port forward
Miniupnpc Socket error
zc|run: got socket error
```

The engine wants to write into its install directory and to open UPnP port forwards. Both are denied or absent here,
which is intended: no ports should be opened to the outside.

## Known limits

- **VPN reconnect / server change:** table 100 is stale afterwards. The engine has no connectivity (fail-closed); rerun
  `acestream-start`.
- **DNS:** name resolution of tracker hostnames may go through your local resolver instead of the VPN. That is a DNS
  leak, not an IP leak. Harden your resolver setup separately if this matters to you.
- **"All traffic through the VPN" mode** of the VPN client makes the leak test report a false positive.
- **Bypassable by yourself:** `acestream-launcher -e <cmd>` and starting the engine by hand as your desktop user
  bypass the launcher guard.
- **WireGuard** is not supported by the script as written.
- The optional split-tunnel feature of a VPN client is no substitute for the nftables rules.

## Rollback

Remove all rules manually if needed:

```sh
sudo nft delete table inet acestream_killswitch
sudo nft delete table inet acestream_input_lock
sudo ip rule delete priority 1000
sudo ip route flush table 100
```

Removing the launcher config (`rm ~/.config/acestream-launcher/config`) re-opens the launcher gap described above.
Removing `~/.config/acestream-launcher/` removes the wrappers and the guard.
