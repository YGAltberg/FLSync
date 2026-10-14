# FLSync phase 4: saving merges, disconnects lose nothing

Goal: everyone ends up with the same project, whoever saves when, and a
dropped connection never loses work. The user's whole model is one key:

> **Ctrl+S = sync the project.** Your changes go to the others, and you get
> theirs. Knobs, faders, steps and plugin parameters are live anyway.

Until phase 3 the newest saved .flp simply replaced the session's project, and
values changed while disconnected were overwritten by the session's when the
connection came back. Phase 4 fixes both:

| Piece | What changes |
|---|---|
| `bridge/flp_merge.py` (new) | three-way merge of .flp files, chunk by chunk, notes and playlist clips one by one |
| `bridge/bridge.py` | merge on save, the version FL has open (the *base*), compare-and-swap shares, opening the merged version, keepalive |
| `relay/relay.py` | `parent` in `snap_put` (compare-and-swap), `ping`/`pong`, versions kept on disk across restarts |
| `fl/FLSync/flsync_client.py` | edits made while disconnected are sent when back; FL's saved/dirty/playing state for the bridge; holding remote values while a save is merged |
| `app/` | the host's relay keeps its versions in the data folder; a session resumes when FLSync starts again |

Protocol stays 2: everything new is optional and ignored by older peers.

## Saving

When FL writes the project file (the bridge watches it, as before), the bridge
compares three versions:

* **base**: the project FL had open before the user's changes: the version FL
  last opened (one the bridge wrote, or merged) or the version this bridge last
  shared. Known by content (sha256) and remembered in `bridge-state.json` in the
  bridge's data folder, so a restart doesn't lose it.
* **mine**: the file FL just wrote.
* **theirs**: the session's newest version (`newest`), downloaded already.

Then:

| Situation | What happens |
|---|---|
| no version in the session yet | share mine (as before) |
| `theirs` is the base (nobody else saved since) | share mine (*fast forward*) |
| mine has nothing new (saved without real changes) | don't share; open theirs (*pull*) |
| theirs has nothing mine lacks | share mine |
| both have changes | merge, share the result, open it in FL |
| the merge is refused (see below) | share mine anyway (newest wins, as before), tell both sides, keep theirs in `Versioner` |
| no base known, others in the session, a version exists | don't share: the project isn't the session's (see "Unrelated projects") |

"Nothing new" ignores what FL changes on every save or that is per computer
(see `EPHEMERAL` in `flp_merge.py`): work time, current pattern, window positions.

Every share sends `parent`: the number of the newest version it includes (the
fast-forwarded or merged `theirs`). The relay refuses a share whose `parent`
isn't its newest version any more (`error`, code `outdated`): someone saved in
between. The bridge then merges again with that version and retries (up to 5
times).

### Where versions go

Received and merged versions are written to
`<projects>\<name>\<name>.flp` (`<projects>` = `Documents\Image-Line\FL Studio\Projects\FLSync`),
`<name>` the session's project name (the newest version's), samples in
`Samples\`, plugin samples next to the .flp, as in phase 2. Before a file there
is replaced, the old one is copied to `<projects>\<name>\Versioner\<name> v<N>.flp`
(the 20 newest are kept). FLSync never writes outside its folders: a user who
started from `Documents\Beat.flp` keeps that file as it was, and works in the
FLSync copy once FL opens a merged or received version.

That copy is FL's working file, so the bridge writes a version over it only
when it holds a version the bridge knows (or the save it is merging right
now), checked again just before the file is replaced: a save of FL's that
isn't shared yet is never written over. A received version then stays in the
cache until the next Ctrl+S merges it in; "Öppna senaste versionen" writes
it when asked (the save goes to `Versioner` first).

### Opening the merged version

The user just saved, so FL has nothing unsaved and reopening loses nothing.
The bridge opens the merged (or pulled) version when FL reports it is **clean**
(no unsaved changes) and **not playing**; while FL plays it waits for the stop.
If FL has unsaved changes by then (the user went on working), it doesn't open
it: the next Ctrl+S merges again and opens the result.

So that values arriving from the others don't make FL "changed" in that time,
FL **holds remote values** from the moment the user saves until the bridge
says how the save went (`saved`, below) or 10 s pass; after a reopen FL takes
the session's values anyway (`sync`), otherwise it writes what it held.

### The others

They hear of the new version as before (`snapshot`); the hint says
"*Anna har sparat – tryck Ctrl+S för att få med ändringarna*". Their Ctrl+S
merges their own changes with it and opens the result. With nothing of their
own it's a pull. A paused FL (another structure) gets the same advice.

An FL that reports nothing unsaved and isn't playing gets the new version
opened right away instead (it loses nothing). So a newcomer whose FL is clean
has the session's project open as soon as it joins.

Opening a version while others are in the session, FL takes the session's
live values (`sync`, as since phase 1): what the live sync carries (knobs,
faders, steps, names, mutes, tempo, ...) is the session's, everything else
(playlist clips, piano-roll notes, automation, plugin states, ...) the
version's. Live values reach the session as they change, so the two agree; a
live value changed only in a file (FL edited with FLSync's script off) is the
session's again once the version is opened.

### Unrelated projects

An FL whose open project has no known base (not opened from a session version
and never shared) can't be merged with the session's version. While others are
in the session and the session has a version, its saves are **not shared**: FL
hears "*Projektet hör inte till sessionen – öppna sessionens version på
FLSync-sidan*". To make your project the session's anyway, click **Dela mitt
projekt** on the FLSync page (it shares the open project, newest wins). Alone in
the session, a save is shared as before.

## The merge (`bridge/flp_merge.py`)

`merge(base, mine, theirs, canon=None, localize=None) -> Merged`, pure (no I/O),
standard library only.

* `canon`: `{"base": {...}, "mine": {...}, "theirs": {...}}`, each mapping a
  sample path as stored in that file (event 196) to a token, the sample's
  sha256 where known. Paths compare by token, else by their text (case and
  slashes ignored). So the same sample at different places on different
  computers counts as the same.
* `localize`: `{theirs path: local path}` for sample paths in parts taken from
  theirs.

`Merged`: `data` (bytes), `mine_new` / `theirs_new` (each side has real
changes the other lacks), `taken` and `conflicts` (Swedish descriptions:
"noter i mönster 3 (Trummor)", "kanal 5 (Serum)", "playlisten"),
`structure` ("same", "mine" or "theirs": whose channels/plugins the result
has), `renumbered` (theirs' new patterns given new numbers).

Raises `MergeRefused(code, text)` (text in Swedish, for the user) when it
can't be done safely. Any doubt about the format is a refusal, never a guess.

### Chunks

An .flp is a header and a flat list of events (`flp.py`). The events are cut
into **chunks** at *anchor* events, each chunk keyed by what its anchor says.
Measured on the 162 projects saved by FL 20-26 that come with FL 2025/2026:

| Anchor | Key | Chunk |
|---|---|---|
| (start) | `head` | project settings, title, groups, controller links, ... |
| 65 *n* before the first 64 | `notes n` | pattern *n*'s notes (224) and controller events (223), 160, 161 |
| any other event after those | `links` | controller links etc. after the notes (226, 227, 230) |
| 64 *n* | `channel n` | channel *n*: type, plugin, state, sample path, ... |
| 65 *n* after the first 64 | `pattern n` | pattern *n*'s name, color, length, time markers |
| 99 *n* | `arrangement n` | name, ... up to its playlist |
| 233 | `playlist n` | the clips of arrangement *n* |
| events between 233 and the first 238 | `markers n` | its time markers |
| 238 | `track n t` | track *t*: 238, 43, 239 |
| any other event after a track | `arrtail` | current arrangement (100) and view settings |
| the events after the previous track's 147 | `insert k` | the *k*-th mixer track (from 0, the master): its color, icon and name (149 42 95 204) come *before* its 236; then its slots (a plugin's events, then 98) and routing, up to its 147 |
| 225 | `mixer` | mixer parameters (225) and the events after it |

FL's order is kept (it isn't always sorted: pattern names can come between
channels; FL's performance templates have a second group of pattern events
after a later channel, `pattern n 1`). The output follows mine's order; a
chunk only theirs has goes after the chunk before it in theirs. The grammar
was checked on all 162 FL 20-26 projects bundled with FL 2025/2026
(flp_merge.py's docstring has the details).

### Per chunk

Compared in **canonical form**: sample paths by token, `EPHEMERAL` events
left out (237 work time, 67 current pattern, 100 current arrangement, 146
current group, 212 plugin window, 159/199/172 FL version, 192 in the head,
200 licensee, 202 data folder). Then, per key: unchanged in theirs -> mine;
unchanged in mine -> theirs; both changed the same -> mine; both changed
differently -> **mine, and a conflict** — except where finer merges apply:

* `head`, `links`, `arrtail`, the events after 225: per event id (all
  occurrences of an id together), `MINE_ALWAYS` ids (version, licensee, data
  folder, work time, current pattern/arrangement/group) always mine.
* notes (224): 24-byte notes as a **multiset**: each note's count is base's
  plus both sides' changes (a change both made counts once). Two people
  editing different notes of one pattern both keep theirs. A missing `notes n`
  chunk counts as no notes. Sorted by position again if all three were.
* playlist (233): clips the same way (80 bytes in FL 24.2+, 60 in 20.99-24.1,
  32 in 20.0-20.8; every clip has 20480 at bytes 4-6 and a track below 500).
  A pattern clip's item is 20480 + its pattern number. Bytes 32-35 of 60/80-
  byte clips are an id FL renumbers on every save: left out of comparisons,
  and theirs' added clips get new unique ids. A pattern exists if it has
  notes, settings or a clip.
* 225: 12-byte records keyed by bytes 4-8, three-way per key.

### Refusals (the structure must allow it)

* PPQ differs, FL major.minor differs between the three, or a file doesn't parse.
* **Channels**: in each side base's channels must be the first ones, same type
  and plugin at the same index (renaming, changing a sample or a preset is
  fine; deleting, moving or replacing a plugin isn't), and only one side may
  add channels. A moved channel of the same type and plugin is caught by its
  name turning up at another index.
* **Mixer**: same number of inserts; an insert that has a name or effects
  turning up at another index means inserts were moved.
* **Patterns / arrangements**: none of base's may be gone (FL renumbers);
  both sides adding the same arrangement number refuses. Both adding the same
  pattern number with different content: theirs' pattern gets the next free
  number, in its chunks and in the playlist clips theirs added.
* **Structure**: a result whose channels come from one side and plugins from
  the other is refused (`structure`): the bridge takes the result's
  fingerprint from the side whose structure it has. Adding mixer tracks (FL
  2024+) is refused for now; FL 27 and later until their format is checked.
* The result must parse, re-cut into the same parts in the same order, have
  channels 0..n-1 in order, every note and clip pointing at an existing
  channel, and the same mixer tracks as mine; the FLhd channel count is set to n.

## The bridge

* **base**: set when FL reports a project load (`opened`, below) and the file
  FL has open (see the next point) has the content of a version the
  bridge wrote or shared (`known` versions, by sha256, with their samples'
  tokens), and when this bridge shares a version (base = what FL saved). A
  merged version becomes the base only once FL has opened it. FL opening a
  file the bridge doesn't know leaves no base.
* **Which file FL has open.** FL keeps its recent-files list in memory (the
  registry has it as of FL's last exit) and its script API has no project
  path. FL's window title names the file ("Beat.flp - FL Studio 2025"); the
  folder comes from, in this order:
  1. a .flp of that name written just before FL said it saved (`st` going to
     0: up to 5 s before, 1.5 s after, the closest first; FL writes the file,
     then clears its flag). `bridge/flpwatch.py` watches the local drives
     (ReadDirectoryChangesW). Not once FL has loaded a project since.
  2. the file the bridge asked FL to open (merged or received version,
     "Öppna senaste versionen"), but only once FL has said it loaded a
     project (`opened`) since: FL may not take it.
  3. the file watched now; the one FL had when the bridge last ran
     (`bridge-state.json`: the launcher may restart while FL keeps it open);
     versions written here; project files whose version is known; FL's
     recent files.
* **Not saves.** A version the bridge writes into the file FL has open isn't
  a save of FL's (nothing is told to FL), and neither is the file turning
  byte for byte into a version the bridge knows: FL stamps every save with its
  work time, so something else copied it there (a synced folder, someone's
  FLSync on the same computer). Neither is shared and the base stays. (Found
  live: such a copy used to move the base to the copied version, and the next
  save then dropped the other's changes that only its file had.)
* `bridge-state.json` in the data folder: the known versions (the newest 50:
  sha256, name, version, sample tokens) and the base per project file.
* The files FL saved and the files written for versions also go into the
  cache by sha256, so a base can always be read back.
* **Keepalive**: `ping` every 5 s on the session connection; nothing from the
  relay for 20 s means it is gone: the bridge drops the connection and FL's
  usual retry joins again.
* Sets for FL are not held by the bridge: FL holds them itself (above).

## FL's script

* **Offline edits**: values FL changes while it isn't in the session (bridge
  or relay gone) are remembered with the value they had before. When FL joins
  again with the same structure: a value the session still has as it was
  before is sent (the change wins); one the others changed meanwhile is a
  conflict and the session's value wins; FL says how many of each. A project
  load or a structure change forgets them. The first FL in an empty session
  shares everything anyway.
* `st {dirty, playing}` to the bridge when either changes: `general.getChangedFlag()`
  (0 clean) and `transport.isPlaying()`. Never forwarded to the relay.
* `opened {fp}` to the bridge after every project load. Never forwarded.
* Holding: when the changed flag goes from nonzero to 0 (a save), remote values
  aren't written into FL until the bridge's `saved` for it arrives (`open:
  "now"` keeps holding until the project load, at most 30 s) or 10 s pass.
* `saved` from the bridge: the outcome of a save, shown in the hint bar.
* `snapshot` notices from the bridge may have `how`: `"save"` (Ctrl+S merges
  it in: the hint says so) or `"open"` (open it on the FLSync page).

## Messages (additions to protocol v2)

Session connection, client -> relay:

* `snap_put` + `parent` (int, optional): the newest version this one includes,
  0 for none. If given and the relay's newest version has another number, the
  relay answers `error {code: "outdated", v: <newest>}` and keeps nothing.
* `snap_put` + `merged` (list of up to 10 names, optional): whose versions it
  includes besides the sharer's own. Passed on in the `snapshot`, like `parent`.
* `ping` -> `pong` (to the sender only). A client that has pinged and then
  sends nothing for 60 s is dropped by the relay.

FL -> bridge only (the bridge never forwards them): `st`, `opened`, `pr` (phase 2).

Bridge -> FL:

* `saved {outcome, v?, with?, open?, why?, conflicts?, taken?}`: `outcome` is
  `shared` (fast forward), `merged`, `pulled`, `unchanged`, `unrelated` (not
  shared), `replaced` (merge refused, newest wins: `why`), `failed` (`why`);
  `with` the names merged with; `open` `"now"`, `"later"` (at a pause; until
  0.15 when playback stops), `"stop"` (when playback stops, 0.16) or `"no"`
  (FL has unsaved changes: save again); `conflicts` how many parts
  both changed (mine kept); `taken` how many parts came from the others.
* `snapshot` notice + `how` (`"save"` or `"open"`), `merged`, `parent`.

## The relay

* Versions on disk: with a store folder of its own (`persist=True`, the
  launcher's `<data>\relay-store`), the relay keeps `session.json` there: the
  version counter, the newest snapshot and the 20 before it. On start it reads
  it back (newest snapshot, its fp as the session's structure, the counter),
  and removes blobs no kept version uses. Values (`state`) are not kept: the
  first FL to join shares its own, as at any start.
* `parent`, `merged`, `ping`/`pong`, idle drop: see Messages.

## FL versions

Two people may run different FL versions (FL 2025 and 2026). FL opens a
project saved by a newer FL after a warning ("saved with a newer version",
OK/Cancel; FL changelog 19930), so:

* Saves merge across versions when the layouts are compatible (flp_merge.py
  decides it from the files: record sizes of the events exchanged); the result
  has the saver's own version stamp (`MINE_ALWAYS`), so their FL opens it
  without the warning. A pull is the merge's result too, for the same reason.
* A version from a newer FL is never opened by itself (the warning would stop
  FL): FL's hint and the page say Ctrl+S brings it in.
* Opening a version uses the FL that runs (`bridge.open_file`): Windows opens
  .flp files with the newest FL installed, which may not be the one in use.
* The launcher reads which FL runs (its program file's version) and checks
  that version's MIDI settings. An FL version whose settings lack FLSync (one
  installed after FLSync) is set up by the launcher as soon as it isn't
  running (FL writes its own settings back when it closes); while it runs, the
  page says to close it. A version never started has no settings yet.

## The launcher

* Hosting uses `<data>\relay-store` (persistent), so a restarted host still
  has the versions.
* Resume: FLSync remembers whether it was in a session when it ended (closed
  window, crash, reboot) and, started again, hosts or joins that session again
  by itself. A guest whose host isn't back yet keeps trying quietly in the
  background (the page says so, with "Sluta försöka"); choosing Starta session
  or Gå med stops it. "Lämna sessionen" forgets it.
* Texts follow the one-key model: "Spara (Ctrl+S) i FL för att synka projektet".
* "Dela mitt projekt" (see Unrelated projects).
