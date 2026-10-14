# FLSync protocol v2

Phase 2 adds what FL's script API can't sync live: the project's content (new
channels and plugins, samples, piano-roll notes, playlist clips, automation).
It travels as **project snapshots**: when someone saves, their bridge uploads
the .flp and the samples it needs, and the others' bridges download them so
their FL can open the new version. Parameters stay live as in v1, but only
between FLs whose projects have the same **structure** (see Structure). `PROTO = 2`.

Changes from v1:
* `hello` and `sets` from FL carry the structure fingerprint `fp`; new `fp`
  message; the relay keeps a session fingerprint and refuses parameter batches
  from another structure (`ack` with `stale`).
* Snapshots: `snapshot` broadcasts, `snap_put`, `reshare`, and a second
  connection per bridge (`app: "files"`) for `blob_*` transfers.
* Step parameters: `gp/<pattern>/<channel>` keys.

## Transport

One JSON object per line over TCP between relay and clients (bridge, monitor),
at most 2 MiB per line.
The bridge forwards session messages to and from FL over MIDI (see
`bridge/wire.py`), which carries about 4 kB/s, so nothing large ever goes to FL:
the bridge handles snapshot transfers itself and forwards only short notices.

Each bridge has two connections to the relay:
* the **session** connection: its FL's `hello` and everything FL sends and
  receives (v1 messages plus `fp`, `snapshot`, `reshare`), and the bridge's own
  `snap_put`;
* the **files** connection (`hello {app: "files"}`): blob uploads and
  downloads, so big transfers never delay the session's messages. A files
  connection isn't a peer: no `peer_join`/`peer_leave`, no broadcasts.

## Keys

`<area>/<index...>/<prop>`. Values are JSON scalars: number, bool, string, null.
Indices are FL's own: mixer tracks, channels, slots and EQ bands from 0
(track 0 = master); patterns (`pt`, `g`, `gp`) and playlist tracks (`pl`) from 1.

| Area | Keys |
|---|---|
| Mixer track `t` (0 = master) | `m/t/v` volume 0..1 (0.8 = 0 dB), `m/t/p` pan -1..1, `m/t/mu` mute, `m/t/n` name, `m/t/c` color (int), `m/t/s` stereo separation -1..1, `m/t/e<b>g` `m/t/e<b>f` `m/t/e<b>w` EQ band `b` gain / frequency / width (0..1), `m/t/fx` effect slots enabled |
| Mixer effect slot `s` of track `t` | `mx/t/s/m` mix level 0..1 (only for slots that hold a plugin; slot on/off isn't synced because FL 2025.2.4's setPluginMuteState can't switch a slot back on) |
| Mixer routing of track `t` | `mr/t` = `"dest:level,dest:level"` sorted by dest |
| Channel `c` (global index) | `c/c/v` volume 0..1, `c/c/p` pan -1..1, `c/c/pi` pitch in whole cents, `c/c/mu` mute, `c/c/n` name, `c/c/c` color, `c/c/t` target mixer track |
| Step sequencer | `g/<pattern>/<channel>` = the channel's row in the pattern as a string of `0`/`1`, one char per step, without trailing `0`s (`""` = no steps on). Not for audio and automation clip channels |
| Step parameters | `gp/<pattern>/<channel>` = the parameters of the row's steps that are on (see Step parameters) |
| Plugin parameters | `pc/<channel>/<plugin>/<param>` for a channel's generator, `pm/<track>/<slot>/<plugin>/<param>` for a mixer effect; value 0..1. `<plugin>` is the plugin's own name with `/` replaced by `\|`, `<param>` the parameter's index from 0 |
| Song | `s/tempo` BPM, `s/num` beats per bar (the denominator can't be read from a script, so it isn't synced), `s/loop` 0 = pattern 1 = song, `s/play` bool, `s/pos` where playback started in quarter notes, -1 while stopped (changes only on play/stop) |
| Patterns | `pt/<i>/n` name, `pt/<i>/c` color |
| Playlist tracks | `pl/<i>/n` name, `pl/<i>/c` color, `pl/<i>/mu` mute |

Plugin parameters are read only from the plugin whose window has focus, and
they are never starting values (far too many for the MIDI link). Others' values
are written whether the plugin has focus or not, but only into the same plugin
at the same place.

`s/play` and `s/pos` are *transient*: the relay stores them like any key, but
clients ignore them in `welcome`/`state` snapshots (joining a session must not
start playback). They only act on them when they arrive in a live `sets`.
Pressing play sends `[s/pos, s/play=true]` in one batch, stop sends
`[s/pos=-1, s/play=false]`.

### Step parameters

`gp/<pattern>/<channel>` lists, for every step of the row that is on, in step
order, `<step>:<pitch>,<velocity>,<release>,<fine pitch>,<pan>,<mod x>,<mod y>,<shift>`
(FL's `midi.pPitch` .. `midi.pShift`, native integer units as
`channels.getCurrentStepParam` returns them), joined with `;`. `""` when no step
is on. Steps are 0-based. A receiver applies it with
`channels.setStepParameterByIndex` after the row's `g` value, since FL only
keeps parameters for steps that are on.

## Structure

Parameter keys address things by number (channel 3, mixer slot 2). They only
mean the same thing in two projects that have the same channels and plugins in
the same places. The FL script therefore computes a **fingerprint** of its
project's structure:

* for every channel (global index order): `channels.getChannelType` and the
  generator's plugin name (`plugins.getPluginName(ch, -1, False, True)`, `""`
  when `plugins.isValid` is false, e.g. a sampler),
* the number of mixer tracks, and for every mixer track and slot that holds a
  plugin: track, slot and the plugin name,

joined into one string and hashed (`fp` = the first 12 hex digits of its 64-bit
FNV-1a hash; see `fl/FLSync/flsync_structure.py`). Channel and track *names*
aren't part of it: renaming is synced live.
The script recomputes it every second and after `OnDirtyChannel` reports new,
deleted or replaced channels.

The relay keeps the **session fingerprint**: that of the first FL to share its
project (the seeder), and from then on that of the newest snapshot. While an FL
is the only FL in the session, its `fp` messages also set the session
fingerprint (it changed its structure or loaded another project while alone):
the relay then clears its state and stored notes and sends it `reshare`, as for
a snapshot. So nobody gets stuck, the same goes for an FL that joins while no
other FL is connected (the session takes its structure, and it gets
`seed: true`), and for the last FL left when the seeder leaves before sharing
(the session takes its structure, and it gets `reshare`); `seed` is handed over
only to an FL with the session's structure.

* An FL whose `fp` differs from the session's doesn't send parameter changes and
  ignores others', and says so in FL's hint bar: either it changed its own
  structure (save to share it) or someone else did (open their version).
* `sets` from FL carry the sender's `fp`. The relay refuses a batch whose `fp`
  isn't the session's (it was made for another structure): none of its pairs
  are stored or forwarded, and the sender gets `ack {l, s, stale: true}`.
  Batches without `fp` (the monitor) are always accepted.
* When a snapshot changes the session fingerprint, the relay clears its state
  (values for the old structure would land on the wrong things) and sends the
  sharer's FL `reshare`: it sends all its values again as normal changes, which
  become the new session state. Others get those values once they've opened the
  new version and their `fp` matches (they ask with `sync`).

## Snapshots

A snapshot is one saved version of the project: the .flp plus the samples it
needs. Versions are numbered by the relay from 1.

**Sharing.** The bridge knows its FL's project file: `--project <path>`, or else
the newest entry of FL's recent-files list (registry
`HKCU\Software\Image-Line\FL Studio <NN>\MRU`, value `0`, highest `<NN>`).
When that file is written (FL saved it) and has been stable for a second, and its
content differs from the last version the bridge shared or received, the bridge:
1. parses it (`bridge/flp.py`) for the samples it uses (and the plugins, for
   the receivers' check). The version's `name` is the file name without `.flp`. Samples under FL's own folders (paths starting with
   `%FLStudioFactoryData%`) exist on every FL and aren't sent; other samples
   are sent when the file exists, up to the relay's size limit;
2. uploads the .flp and every sample the relay doesn't have yet over its files
   connection (`blob_has`, `blob`);
3. sends `snap_put` on its session connection.

**Receiving.** On `snapshot` (or a `snapshot` in `welcome`) from someone else,
the bridge downloads the .flp and the samples it doesn't have in its cache,
writes the project folder `<Documents>\Image-Line\FL Studio\Projects\FLSync\<name>\`
with the samples in `Samples\` and the .flp's sample paths rewritten to point
there (and the project data folder, event 202, emptied: FL stops with a dialog
if that folder doesn't exist). Each file is written to a temporary name and
moved into place, the .flp last; a sample whose name is taken by different
content becomes `name (2).wav`. The bridge tells its FL twice: a short
`snapshot` notice at once (FL must switch to the new session fingerprint before
the sharer's re-sent values arrive), and the same notice with `path` (and
`missing` plugins) once the folder is written, if that version is still the
newest. With
`--auto-open` it also opens the .flp in FL. Names from the network are
sanitized before they become file names, and nothing is written outside that
folder and the cache.

**Opening** a snapshot is a project load in FL: the script sees
`OnProjectLoad`, its `fp` now matches the session's, and it asks for the
session's current values with `sync`.

## Notes (piano roll)

FL's MIDI-script API can't read or write piano-roll notes. Piano roll scripts
(`flpianoroll`) can, but only when the user runs one, and they run in their own
Python: they can't reach the FLSync script. Unlike MIDI scripts they may read
and write files (verified in FL 2025.2.4; sockets are blocked there too). So the
bridge is the go-between, through an exchange folder
`%LOCALAPPDATA%\FLSync\notes\`:

* The FLSync script tells its bridge which pattern and channel the piano roll
  shows: `pr {p, c, n}` (active pattern, selected channel's global index, its
  name) whenever they change. The bridge keeps this to itself.
* The bridge keeps `context.json` there up to date:
  `{"pattern", "channel", "name", "session": {"v", "by", "ppq", "notes"} | null}`
  with the session's newest notes for that pattern and channel.
* **"FLSync skicka noter"** (a piano roll script; Tools -> Scripts, repeat with
  Ctrl+Alt+Y) writes all notes of the piano roll to `outbox.json`:
  `{"pattern", "channel", "ppq", "notes"}` with pattern and channel copied from
  `context.json`. The bridge picks it up, deletes it and sends `notes_put`.
* **"FLSync hämta noter"** replaces the piano roll's notes with the session's
  from `context.json`, times scaled from their PPQ to the project's.
* A note is `[time, length, number, velocity, pan, release, color, fcut, fres,
  pitchofs, slide, porta, muted, repeats]` (`flpianoroll.Note` fields; ticks,
  floats 0..1, booleans as 0/1).
* The relay keeps the newest notes per pattern and channel (cleared when the
  session fingerprint changes, like the parameter state) and sends them to
  everyone; bridges tell their FL in short (`notes {p, c, by, n}`), so the
  hint bar can say that new notes are ready to fetch.

## Messages

Client -> relay (session connection):

| Message | Meaning |
|---|---|
| `hello {name, app, proto, api?, fp?}` | first message; `app` is `"fl"` or `"monitor"`; FL sends its structure fingerprint |
| `sets {l, kv, seed?, fp?}` | a batch of changes: `kv` is `[[key, value], ...]` (1..256 pairs), `l` the sender's batch number, `seed: true` for starting values, `fp` the structure it was made for |
| `set {k, v, l}` | a single change; same as `sets` with one pair (kept for simple tools) |
| `sync {}` | ask for the full state |
| `fp {fp}` | FL's structure fingerprint changed |
| `snap_put {name, fp, flp, size, samples, plugins}` | (bridge) a new version is uploaded: `flp` = sha256 hex of the .flp, `samples` = `[{h, size, path}]` (`path` = the path text in the .flp), `plugins` = plugin names it uses. Sent after every blob it refers to is on the relay |
| `notes_put {p, c, ppq, notes, fp}` | (bridge) the notes of pattern `p`, channel `c` from the piano roll script (at most 20000 notes of 14 finite numbers); if `fp` isn't the session's it's refused with `error {msg, stale: true}` |

Client -> relay (files connection):

| Message | Meaning |
|---|---|
| `hello {app: "files", proto, name}` | first message; answered with `welcome {proto, id, name, max_blob}` |
| `blob_has {h: [hash, ...]}` | which of these the relay is missing; answered with `blob_missing` |
| `blob {h, i, n, d}` | chunk `i` of `n` (from 0, in order) of blob `h`, `d` = base64 of at most 512 KiB; answered with `blob_ok {h}` after the last chunk if the sha256 matches, else `error {msg, h}` once (its later chunks are ignored; a chunk with `i = 0` starts over) |
| `blob_get {h}` | download a blob; answered with its `blob` chunks, or `error` if unknown |

Relay -> client:

| Message | Meaning |
|---|---|
| `welcome {proto, id, name, seed, state, peers, s, fp, snapshot, notes}` | answer to hello. `seed: true` asks this FL to share its project's values as starting values. `fp` = session fingerprint (null before the first FL), `snapshot` = the newest snapshot (the fields of `snapshot`, without `t`) or null, `notes` = the stored notes as a list of `{p, c, v, by, from, ppq, notes}` |
| `sets {from, kv, s}` | someone else's accepted batch (sent to everyone except the sender) |
| `ack {l, s, stale?}` | to the sender, after its batch `l` was handled; `stale: true` if it was refused for another structure |
| `keep {l, kv}` | to the sender of a seed batch: pairs whose keys someone already changed; the sender must take these values |
| `reject {l, k, msg}` | one invalid pair (bad key/value); the rest of the batch still applies |
| `state {state, s}` | answer to `sync` |
| `seed {}` | the FL asked to share starting values left; you share yours instead |
| `snapshot {v, name, fp, flp, size, samples, plugins, by, from}` | a new version (to everyone, the sharer included): `by` = sharer's name, `from` = its client id. A bridge forwards it to FL shortened to `{t, v, name, fp, by, from}` plus `path` once the project folder is written |
| `reshare {}` | to the sharer's FL after its snapshot changed the session fingerprint: send all values again (as normal changes) |
| `notes {p, c, v, by, from, ppq, notes}` | someone's notes for pattern `p`, channel `c` (to everyone, the sender included; `welcome` has the stored ones as `notes: [...]`). A bridge forwards it to FL as `{t, p, c, by, from, n}` (`n` = number of notes) |
| `peer_join {id, name, app}`, `peer_leave {id, name, app}` | presence |
| `error {msg}` | protocol error; for a wrong `proto` in hello the relay sends this and closes |
| `blob_missing {h: [...]}`, `blob_ok {h}`, `blob {h, i, n, d}`, `error {msg, h}` | files connection answers (`h` null if the request's hash was invalid) |

The bridge also sends FL `link {up: false, msg}` when the relay can't be reached.

## Phase 3 additions

Additive; `PROTO` stays 2 (see docs/phase3.md for the why):

* Every `hello` may carry `pw`. A relay started with `--password` answers a
  missing or wrong one with `error {code: "password", msg}` and closes (files
  connections too).
* FL's `hello` carries `flv`, FL's own version text (`ui.getVersion` modes
  0-3 joined with dots, e.g. `"25.2.4.4960"`); peer info (`welcome.peers`,
  `peer_join`, `peer_leave`) carries it as `flv`.
* `pen {id, name, busy?}` / `pen {id: null}` from the relay; `welcome.pen` =
  `{id, name}` or null. An FL among others whose `fp` leaves the session's
  takes the pen if it's free (broadcast); if someone else has it, that FL alone
  gets `pen {..., busy: true}`. Freed when the holder's structure is the
  session's again (its snapshot, its `fp`, a restructure) or it leaves.
* When an FL leaves and exactly one FL with another structure is left, the
  session takes that FL's structure (state and notes cleared, `reshare` to it),
  and a stored snapshot of another structure isn't handed to newcomers anymore.
* Bridge -> FL: `loaded {v, fp}` once FL has opened the project the bridge
  wrote for version `v`. FL then reports `fp` (the version's fingerprint) as
  its own until FL's own reading of its structure changes; so a plugin missing
  on one computer doesn't pause it.
* The bridge's `snapshot` notice to FL: `missing` is a list of plugin names or
  `{plugin, channels: [n, ...]}` / `{plugin, mixer: true}`; `newer` = the FL
  version the project was saved with when its major.minor.release is newer
  than this FL's; `flv` = that version.
* `snap_put` may carry `flv` (the FL version that saved the .flp); the relay
  keeps it in the snapshot. A sample entry with `plugin: true` is a file named
  inside a plugin's state (FPC, DirectWave, Slicex, ...): receivers put it next
  to the .flp, where FL looks for it by name, and don't rewrite its path.
* FL's `fp` message carries `load: true` when the change comes from opening a
  project (or a version, via `loaded`) rather than from editing one: such a
  change never takes the pen.
* A wrong protocol version in `hello` is answered with `error {code: "proto", msg}`.

## Phase 4 additions

Additive too; `PROTO` stays 2 (see docs/phase4.md for the why: saves merge,
disconnects lose nothing):

* `snap_put` may carry `parent`: the number of the newest version it includes
  (0: none). The relay takes it only if that is still its newest version (or
  it has none and `parent` is 0); else it answers `error {code: "outdated", v:
  <its newest version or 0>, msg}` and keeps nothing. The bridge then merges
  with that version and shares again. Without `parent` a `snap_put` is taken as
  before.
* `snap_put` may carry `merged`: up to 10 names (at most 40 characters each)
  whose versions it includes besides the sharer's. `parent` and `merged` are
  kept in the snapshot and passed on in `snapshot` and `welcome.snapshot`.
* `ping` -> `pong` (to the sender only). A session client that has sent a
  `ping` and then sends nothing for 60 s is dropped. The bridge pings every
  5 s and drops a relay it hasn't heard a byte from for 20 s.
* A relay with `persist` (the launcher's host) keeps `session.json` in its
  store folder: the version counter, the newest snapshot and the 20 before it.
  Started again it has the newest version (and its structure) for newcomers;
  values aren't kept.
* FL -> bridge only, never forwarded: `st {dirty, playing}` (FL's
  `general.getChangedFlag()`, 0 = nothing unsaved, and `transport.isPlaying()`,
  sent when they change) and `opened {fp}` (after every project load, with FL's
  own reading of its structure).
* Bridge -> FL: `saved {outcome, v?, with?, open?, why?, conflicts?, taken?}`
  after each save: `outcome` `shared`, `merged`, `pulled` (nothing new: the
  newest is opened instead), `unchanged`, `unrelated` (FL's project isn't the
  session's: not shared), `replaced` (couldn't be merged, `why`; shared
  anyway) or `failed` (`why`); `with` the names merged with; `open` `"now"`,
  `"later"` (when playback stops) or `"no"` (FL has unsaved changes again).
  FL holds remote values from its save until `saved` comes (at most 10 s; with
  `open: "now"` until the project load, at most 30 s).
* The bridge's `snapshot` notice to FL may carry `how`: `"save"` (Ctrl+S
  merges it in) or `"open"` (FL's project isn't one the bridge can merge: open
  the version), and `merged` (names).

## 0.10 additions

Additive; `PROTO` stays 2. Older peers keep working, they just pause more.

* **Two-part fingerprint.** FL's `fp` is still 12 hex digits: the first 6
  hash the channels (type and instrument per channel), the last 6 the mixer
  (track count and the plugin per effect slot). Keys of `c/ g/ gp/ pc/` depend
  on the channel part, `m/ mx/ mr/ pm/` on the mixer part, all others (`s/`,
  `pl/`, `pt/`) on neither. A fingerprint of another length differs as a
  whole. The relay refuses only the pairs of a batch whose part differs from
  the session's: `ack {l, s, stale: [keys]}` (or `stale: true` if all were).
  FL sends and applies a key whenever its part matches, so a new instrument
  pauses only the channels and a new effect only the mixer. When the session
  takes a new structure the relay keeps the values of the parts that stayed
  (and the notes if the channels did).
* **`note {kind: "unrelated", name}`** (bridge -> relay): FL saved a project
  that isn't the session's, which wasn't shared. The relay passes it to the
  others as `note {kind, name, from, by}` and keeps nothing.
* **Switching the session's project.** Saving such a project again within
  15 s (or "Dela mitt projekt") shares it with `switch: true` in `snap_put`;
  the relay keeps it in the snapshot and starts all values over. A bridge that
  gets someone else's switch forgets FL's base, and a save of the old project
  isn't shared or merged: `saved {outcome: "old", with, open}`, and FL opens
  the new project. The sharer gets `saved {outcome: "switched"}`. An
  `unrelated` answer carries `confirm: 15`.
* **A busy FL stays.** FL's script is silent while FL loads a plugin or a
  project, saves or shows a dialog. The bridge keeps its relay session while
  FL's main window is open, holds its messages to FL meanwhile, and leaves
  only after 120 s or when the window is gone. When it hears FL again after
  having left, it sends FL `link {up: false}`, so the script joins again. The
  script itself takes a gap of 2 s or more between its ticks for its own
  stall (`MidiLink.forgive`), not for a lost bridge.
* After the link lost events on the way to FL, the script asks for `sync`
  (at most every 30 s). FL -> bridge `log {data}` lines (stalls, failing
  areas, pauses, what the FL offers scripts) go to FLSync's log.

## 0.11 additions: content without Ctrl+S

Clips, notes, samples, new channels and plugins and their state can't be
keys (FL's script API can't read or write them; FL 2026's `ilmcp` module,
which Gopher uses to add channels and effects, can't be imported by a MIDI
script: measured in 26.1.7). They travel as versions, and FL saves them by
itself (flsync_content.py):

* The script counts *content*: an undo step (`general.getUndoHistoryCount/
  Pos`, or OnRefresh `HW_Dirty_Undo` with few flags; FL refreshes everything
  with 11 flags as the script starts or a project loads) with no live change
  within 1 s of it (FL 26.1.7 makes the script's own writes undo steps too),
  more patterns, a longer song, a new structure, a change of a key in a
  paused part.
* Bridge -> FL `autosave {ok}`: FL may save by itself. Only in a session
  with other FLs, when FL's project is the session's (a known base) and a
  writable file of its own that FL's title names, not under FL's own
  folder. `{ok: false, broken: true, retry}`: FL said it would save and no
  save came within 15 s (a dialog was open, or FL was busy loading). With
  `retry: true` the bridge offers `{ok: true}` again after 60 s; after 3
  misses in a row `retry: false`, and it is never asked again in this run of
  the script. A bridge before 0.14 sent no `retry`: the script gave up at once.
* The bridge removes the copies FL's Backup folder
  (`<user data>\FL Studio\Projects\Backup`, `<name> (overwritten on ...).flp`)
  gets for FL's saves by itself, keeping one per project file per 10 minutes
  and always the newest (FL's File > Revert to last autosave opens it): FL
  keeps only its newest copies of all projects together (100 by default), so
  saves every few seconds would push out everything else's. A save's copy is
  the first of the project's name from 1 s before its `autosave` to 5 s after
  (a Ctrl+S right after makes its own, later one); the one before the newest
  goes when the next comes, only if its time and size are still as seen.
* FL -> bridge `autosave {content, pull}` right before FL saves by itself
  (`transport.globalTransport(FPT_Save, 1, PME_System, GT_Global)`, from
  OnIdle only, FL 26.1.7 or later, never while recording): once content > 0
  and nothing changed for 2 s, at most every 5 s.
* The bridge takes the next save after `autosave` for that one. With
  content 0, or when `flp_merge.content_changes(base, save)` finds only live
  values changed, it isn't shared: `saved {outcome: "unchanged"}`, or, if
  someone shared a newer version, the save is merged with it locally and FL
  opens the result (`pulled`). No version goes round in circles.
* Bridge -> FL `pull {by, v}`: the newest version isn't what FL has and FL
  can't just open it (unsaved changes; writing others' values makes them).
  Not for a version whose plugins are missing here or from a newer FL. FL
  saves once it is stopped, not recording and nothing changed for 2 s, after
  a 3 s countdown in the hint bar ("hämtar Kims ändringar om 3 s"); any
  change here starts it over. That save brings the version in as above.
* **New channels live (0.13, samplers 0.15).** FL, asked to open a channel
  preset (an .fst: FLhd format 0x20, event 199 FL's version, then all the
  channel's events from its 64 on, as FL's own Data\Patches\Channel presets
  are) with the FL that runs, adds a channel just like it at the end of its
  channel rack, without loading the project and also while it plays:
  measured in FL 26.1.7 with a sampler (its sample loaded from the path in
  its 196), FLEX and the VST3 Serum 2 (their 213 state), every event equal
  but the channel's number, its plugin window's place (212, EPHEMERAL), its
  cut groups (132: FL 26 gives every channel it adds the next number of its
  own, so the sharer's FL gave the same in the same project; the preset's
  copy came from another channel), its mixer track (104: FL puts it on the
  master; live sync's `c/N/t` brings the sharer's) and the name (FL names it
  after the file, as for plugin presets; `c/N/n` brings the sharer's). 0.13
  used plugin presets (format 0x30: 199, 201, 212, 203, 213), which leave the
  channel's own settings at FL's defaults. 0.13 and 0.14 also left cut
  groups out of `content_changes` because of a test peer that copied a
  channel's; a cut group is content again (0.15): should two FLs ever number
  one differently, the difference is shared once, and then they agree.
  Effects and mixer presets opened that way always go to the master track,
  so they aren't used; layers, audio clips' and automation clips' channels
  point at more of the project, so they come with the version. When a
  version arrives whose only change from FL's base is ONE channel added
  after all others (`flp_merge.appended_channels`, and `content_changes` has
  nothing else; 104 is live), FL's channels are the session's from before,
  and the channel's samples are files here (its 196, and the paths in its
  plugin's state, as FPC and DirectWave keep them, but FL's own: else FL
  stops at a dialog about it; a plugin's own samples keep the sharer's
  paths, so such a channel comes with the version, which FL finds them for
  by name next to it), the bridge writes the channel as a preset
  (`flp_merge.channel_preset`, from the version as written here, so its
  sample path points at this computer's copy) to `<version
  folder>\Kanaler\<name>.fst`, sends FL `adding {fp}` and opens it. Two or
  more new channels come with the version: FL would show a structure between
  them, and two presets could be opened out of order or share a name. FL's script takes that structure as
  someone else's (`fp` with `load`, no pen, no content for 20 s or until 2 s
  after `added`). Once FL's channels are the version's, the bridge sends
  `added {by, what}` and the version is FL's base; else it comes in by
  loading (after `LIVE_CONFIRM` 15 s). The sharer's FL saves a new channel
  or plugin 1 s after it came, whatever else goes on, so it gets there fast.
* The bridge paces MIDI to FL by the clock (4000 events/s, bursts of 120).
  A hello while the welcome is still being sent doesn't restart the
  session; FL doesn't say hello again while a message is coming in.

## Ordering and conflicts

* The relay handles messages one at a time. Every accepted pair gets the next
  sequence number `s` and is stored as the latest value for its key.
* For a batch the relay sends, in this order: `reject`s (if any), `keep` (if
  any), then `ack` to the sender, and `sets` with the accepted pairs to everyone
  else.
* A client keeps a key *pending* from the moment it sends a change until the
  `ack` (or `keep`/`reject`) for that batch arrives. While a key is pending the
  client ignores other people's values for it: its own change is later in the
  relay's order and wins everywhere.
* Seed pairs are applied only to keys nobody has changed with a normal pair.
  Pairs for keys that were changed come back in `keep`, and the sharer takes the
  session's value. Normal pairs always apply and mark their keys as changed.
* Snapshots: each one builds on the one before (`parent`, compare-and-swap at
  the relay). A save made while someone else shared is merged with that version
  first (bridge/flp_merge.py); where both changed the same thing the later
  saver's change is kept. Two versions that can't be merged (both added
  channels, different FL releases): the later save replaces the newest, which
  stays in the project folder's `Versioner`.
* Validation: key a non-empty string of at most 256 characters; value null,
  bool, finite number or string of at most 4096 characters; at most 256 pairs
  per batch. Blobs: sha256 hex names, at most `--max-blob` bytes (default
  256 MiB) each; every blob a `snap_put` names must be stored with the size it
  gives; `blob_has` and `snap_put` lists at most 4096 entries. Notes: at most
  20000 per `notes_put`, 60000 stored in all (the oldest pattern/channel goes
  first).
