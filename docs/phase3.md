# FLSync phase 3: easy setup, every use case

Goal: two people who have FL Studio and nothing else can get a session going
with two double-clicks each, over a LAN or the internet, and the session keeps
working when their computers differ (plugins, FL versions, samples).

## Pieces

| Piece | Who uses it | What it does |
|---|---|---|
| `FLSync.exe` + `setup/setup.py` | everyone, once | from the download: installs everything in FLSync's window, see "Setup" and "FLSync's window" |
| `FLSync.exe` + `app/flsync.py` | everyone, every session | the launcher: relay (when hosting) + bridge + its UI in a window of its own |
| FL script, piano roll scripts | inside FL | as in phases 1-2, plus the additions below |

(Until 2026-10-09 these were `Installera FLSync.cmd` and `FLSync.cmd`, with a
console and the UI in the default browser; they remain for developers.)

Everything runs on **FL's own Python** (`<FL>\Shared\Python\python.exe`, 3.12,
no tkinter, has asyncio/ssl/ctypes/winreg) or any Python 3.10+: nobody has to
install Python. Windows only (the bridge uses WinMM; macOS is not supported).

## Setup (`setup/setup.py`, run in FLSync's window, or by `Installera FLSync.cmd`)

Runs with FL closed (it says so and waits, or exits, if FL64.exe runs). Idempotent:
running it again repairs/updates. No admin rights except what winget asks for.

1. **Find FL**: every installed FL Studio (`HKCU\Software\Image-Line\FL Studio <NN>`,
   and install folders `C:\Program Files*\Image-Line\FL Studio *`), and its Python.
2. **loopMIDI**: if not installed, offer to install it (`winget install --id
   TobiasErichsen.loopMIDI -e` — check the id with `winget show`), else open the
   download page. Then add the port: registry `HKCU\Software\Tobias Erichsen\loopMIDI\Ports`,
   DWORD value `FLSync` = 1 (measured on this PC: that's how loopMIDI stores
   its ports), and (re)start loopMIDI so the port exists before FL starts (ports
   created while FL runs carry no data). loopMIDI's exe is found via its
   uninstall entry / `Program Files (x86)\Tobias Erichsen\loopMIDI`.
3. **Install the app**: copy the program (`app/`, `bridge/`, `relay/`, `tools/`,
   `fl/`) to `%LOCALAPPDATA%\FLSync\app` so the downloaded folder can go away;
   copy `fl\FLSync` to `<Documents>\Image-Line\FL Studio\Settings\Hardware\FLSync`
   (replacing an older copy; a junction to a dev checkout is left alone with a note)
   and the piano roll scripts to `...\Settings\Piano roll scripts`.
4. **Configure FL's MIDI settings** for every FL version found (measured on this
   PC, all values REG_SZ):
   - `HKCU\Software\Image-Line\FL Studio <NN>\Devices\MIDI input\FLSync`:
     `Type`="-3", `ScriptFolder`="FLSync", `IDString`="-", `Port`="<n>", `Enabled`="1"
   - `...\Devices\MIDI output\FLSync`: `Port`="<n>", `Sync`="0", `SendAllNotesOff`="1", `IDString`="-"
   - `<n>`: keep FLSync's current port if it has one, else the lowest number
     >= 10 that no other device under `MIDI input`/`MIDI output` uses.
5. **Shortcut**: "FLSync" on the desktop and in the Start menu, running the
   installed `FLSync.exe`; "Reparera FLSync" (`--repair`) and "Avinstallera
   FLSync" (`--uninstall`) in the Start menu, the same exe.
6. `--uninstall` undoes 3-5 (leaves loopMIDI installed, removes the FLSync port).

## FLSync's window (no console, no browser tab)

- **`FLSync.exe`** (`launcher/FLSync.cs`, C# 5 for the .NET Framework 4 compiler
  every Windows has; `scripts/build_exe.py`, called by `make_zip.py`): finds the
  newest FL's `Shared\Python\pythonw.exe` like the installer does and starts
  `app\flsync.py` with it, no console (`CreateNoWindow`). Started anywhere but
  `%LOCALAPPDATA%\FLSync\app` (the unpacked download) it adds `--install`.
  `--launcher-check` prints the command instead. Errors: a message box.
- **The window**: Edge (every Windows 10/11; else Chrome) with `--app=<page>` and
  its own profile in `<data>\window`: no tabs, no address bar, a program of its
  own. Timer throttling is off (`--disable-background-timer-throttling` ...):
  the page polls `/api/status` every second also when minimized. No Edge or
  Chrome: the default browser. `--browser` asks for that, `--no-browser` for no window.
- **Closing it ends FLSync**: its browser process ending (unless within 5 s:
  then it handed the window to an Edge that ran already), or the page's
  `navigator.sendBeacon("/api/bye?t=<token>")` on `pagehide` with no status
  poll within 3 s after (a reload polls again at once). Ending FLSync closes the
  window (WM_CLOSE, then kill). "Avsluta FLSync" also calls `window.close()`.
- **One FLSync**: a second start finds the first (`launcher.json` has the
  window's process) and brings its window forward (`SetForegroundWindow`; the
  new process may, the user just started it), else asks it for a new window
  (`POST /api/window`). `--install`, `--repair` and `--uninstall` end a running
  FLSync first (`POST /api/quit`, then wait for its lock).
- **The installer in the window** (`app/installer.py`): `setup.Setup` on a
  worker thread with `window=True`: its lines and questions (`confirm`) go to
  the page (`status.setup`; `POST /api/setup {action: begin|answer|cancel|launch}`),
  Avbryt stops the wait for FL to close, console programs (reg, PowerShell,
  winget, taskkill) get `CREATE_NO_WINDOW`. When done, "Starta FLSync" starts
  the bridge in the same process and window.
- **Without a console** (pythonw), what would be printed goes to
  `<data>\flsync.log` (kept under 1 MB, the previous one as `.old`).

## The launcher (`app/flsync.py`)

One process, one asyncio loop: the relay when hosting, the bridge always, and a
web UI on `http://127.0.0.1:<port>` (shown in FLSync's window, see above).
Settings (name, last invite, mode) in `%LOCALAPPDATA%\FLSync\settings.json`.

**Host ("Starta session")**: starts the relay on all interfaces with a random
password, tries UPnP (stdlib SSDP + SOAP to the router: AddPortMapping TCP
31415, GetExternalIPAddress; removed again on exit), and shows an **invite code**
to send to the others. The code holds the address(es) and the password, e.g.
`FLS1-` + base32 of `host:port:password` + checksum, grouped for reading
aloud; LAN address, public address (UPnP) and a Tailscale address (100.x) if
present. The host's own bridge connects to `127.0.0.1`.

**Join ("Gå med")**: paste an invite code; the bridge tries its addresses in
order (LAN, Tailscale, public) and keeps the first that answers.

**Status page**: connection (relay, FL heard on MIDI, joined), participants with
their FL versions, the session's newest version (who, when, samples, missing
plugins), notes received, a log; buttons: "Öppna senaste versionen" (opens the
written .flp with the OS; if a second FL64.exe appears, it warns that FL
opened a new window and how to open the file instead), "Kopiera inbjudan",
"Lämna". Diagnostics with concrete fixes: loopMIDI not running / no FLSync port
/ FL not running / FL running but not heard (MIDI settings: check the registry
values above, FL must be restarted after setup) / relay unreachable / wrong
password / other protocol version.

## Protocol additions (still `PROTO = 2`; all optional fields)

* `hello` from FL gains `flv` (FL's version text, `ui.getVersion()`); every
  `hello` may carry `pw`. A relay started with a password (`--password`) answers
  a missing or wrong `pw` with `error {msg, code: "password"}` and closes;
  files connections too.
* Peer info (`welcome.peers`, `peer_join`, `peer_leave`) gains `flv`.
* **Pen** (one structure change at a time): when an FL that isn't alone reports
  an `fp` other than the session's, the relay gives it the pen if nobody has it
  and broadcasts `pen {id, name}`; if someone else has it, that FL gets `pen
  {id, name, busy: true}` (their change may be overwritten when the holder
  saves). The pen is freed (`pen {id: null}`) when its holder's snapshot is
  accepted, its `fp` is the session's again, or it leaves. FL shows it in the
  hint bar ("Anna ändrar projektets struktur").
* **Loaded**: the bridge tells FL `loaded {v, fp}` when FL has opened the
  project folder it wrote for version `v` (FL's recent-files list or its window
  title names that .flp). FL then counts its current structure as version `v`'s
  even if it reads differently here (a plugin missing on this computer, another
  FL version), until its structure changes again. So a missing plugin doesn't
  pause anyone: that plugin's parameters just don't apply here.
* The bridge's `snapshot` notice to FL gains `flv` (the FL version that saved
  it) and `missing` becomes `[{plugin, channels: [n, ...]}]` (channels from 1),
  when the bridge can tell.

## Content additions (bridge + `bridge/flp.py`)

* Per channel: index, name, plugin (`Project.channels`), so missing plugins can
  be named with the channels that use them.
* FL version check: the snapshot's FL version vs this FL's (`flv` from FL's
  hello): newer than ours -> warning ("sparad i FL 26.1.7, du har 25.2.4: FL
  kan inte öppna den förrän du uppdaterar").
* Samples inside plugin states (FPC, DirectWave, Slicex, Fruity Slicer,
  Convolver, ...): find absolute paths of audio files inside their state data
  (UTF-16LE and UTF-8 strings ending in an audio extension), ship the files
  that exist, and put them in the received project's own folder (FL looks there
  by file name). Their paths inside the state aren't rewritten.
* Detecting that FL opened a received version (for `loaded`): FL's MRU value 0
  equals the written .flp, or FL's main window title contains its name.
