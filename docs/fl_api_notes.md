# FL Studio scripting API notes for FLSync (target: FL Studio 2025.2.4 = MIDI-scripting API v40, Python 3.12)

Legend: `uGI` = `useGlobalIndex`; `[vN]` = API version that added the function (none = v1); **NEW40** = exists in API 40 but is missing from the stubs; **>40** = documented for newer API only,
absent from 25.2.4; `(m)` = number/signature from the Image-Line manual; `(proj)` = taken from this project's own code, comments or test fakes (some are live observations such as the tempo units, some are
assumptions; I did not verify them); **UCS** = the open-source Universal Controller Script. "unverified" = needs a live test (section 11). Nothing here was executed inside FL.

## Most important caveats

1. **Channel index = GROUP index unless told otherwise.** The channel-index argument of every `channels.*` (`useGlobalIndex` flag since v33) and `plugins.*` (since v26) function is relative to
   the channel-rack group the user has selected right now, and `channelCount()` is group-filtered. For FLSync: always `useGlobalIndex=True`, passed by keyword, and iterate
   `range(channels.channelCount(True))`. Global-only: `channelNumber()`, `midiNoteOn()`. Group-only (no flag): `mixer.linkChannelToTrack()`, probably the generator id of
   `ui.getFocusedFormID()`. Details in 1.1.
2. **Steps: only the active pattern is reachable.** `getGridBit/setGridBit/getGridBitWithLoop/getCurrentStepParam` act on `patterns.patternNumber()`. Only
   `setStepParameterByIndex(index, patNum, ...)` takes a pattern (1-based) and it writes step *parameters*, not on/off. Another pattern's on/off bits need
   `patterns.jumpToPattern(n)`, which changes the user's active pattern and creates pattern n if it does not exist. There is no API for piano-roll notes, playlist
   clips, or adding/removing/moving channels, plugins, mixer tracks or patterns (except the pattern calls in section 5).
3. **No callback says WHAT changed.** `OnRefresh` flags (`HW_Dirty_Patterns`, `HW_Dirty_ControlValues`, ...) and `OnDirtyChannel/OnDirtyMixerTrack` only say "something in this
   area". Plugin parameters have no per-parameter change event (just the generic `HW_Dirty_ControlValues` flag), and nothing returns the "last tweaked parameter" (only
   `device.linkToLastTweaked` creates a link). Poll and diff.
4. **Focused plugin.** Test `ui.getFocused(midi.widPluginEffect / widPluginGenerator)` BEFORE reading `ui.getFocusedFormID()` (its values collide with window ids 0-4).
   Effect: track = `id >> 22`, slot = `(id >> 16) & 0x3F` (or `mixer.getActiveEffectIndex()`). Generator: id = channel index, GROUP-relative according to UCS (so `channels.getChannelIndex(id)`),
   assumed GLOBAL by FLSync's code (which tries both and checks `ui.getFocusedPluginName()`); unverified on 2025. Section 4.
5. **Parameter counting.** VST `getParamCount` = 4240 = 4096 slots (unused ones have name `""`) + 128 MIDI-CC pseudo-params + 16 aftertouch. Real parameters =
   `idx < 4096 and getParamName(...) != ""`. `getParamDefaultValue` does not exist. Section 3.
6. **Song settings in API 40.** Tempo: get + set exist (`mixer.getCurrentTempo/setCurrentTempo`) but the units misbehave: (proj, live) `getCurrentTempo()` returned BPM*1000 and `getCurrentTempo(True)` BPM,
   the reverse of the docs; FLSync sets tempo via `REC_Tempo`. Time signature: SETTERS only (`general.setNumerator/setDenominator/setRecPPQ`); numerator = `getRecPPB() // getRecPPQ()`, the denominator
   cannot be read. Swing and master pitch only via REC events (`REC_MainShuffle`, `REC_MainPitch`); `channels.getShuffle/setShuffle` are API 42 (>40). Section 8.
7. **Stubs v37 are stale for 25.2.4**: `soloChannel(index, value, uGI)` has `value`; `plugins.getParamValueString` has no `pickupMode`; `plugins.getColor/getName` have `paramIndex`
   before `uGI`; `OnDirtyChannel(index, flag)` takes TWO args; the stubs' mixer REC constants and `REC_MIDIController` differ from FL's own `midi.py`, which is what scripts import.
   Prefer keyword arguments for `uGI` and positional for everything else. Appendix B.
8. **Setters can raise** `TypeError("Operation unsafe at current time")` (check `general.safeToEdit()`, v29). Many calls TOGGLE by default (`transport.start/record/setLoopMode`,
   `muteChannel/muteTrack/soloX` with `value=-1`): read first or pass the explicit value. Some read-backs lag until FL's next refresh (proj: `isTrackMuted`, `isChannelMuted`,
   `playlist.isTrackMuted`, `transport.isPlaying`, `getPluginMixLevel`): remember what you asked for instead of re-reading at once.
   Engine error strings: "Operation unsafe at current time", "Index out of range", "Range error", "Plugin not valid", "Wrong number of function parameters or wrong parameter type".
   A bad mixer track number only prints a one-time warning ("Invalid mixer track nr N ... only displayed once per session"). Which of these raise vs print is unverified: wrap writes in try/except.
9. **Dynamic mixer (FL 2025).** Inserts are created on demand (capacity 500). (proj) `trackCount()` = tracks that exist (master + inserts, NOT "Current"), `getTrackInfo(TN_LastIns)` =
   capacity 500, `getTrackInfo(TN_Sel)` = 501 = "Current". Loop `range(min(mixer.trackCount(), mixer.getTrackInfo(midi.TN_Sel)))`. `getTrackCount()` (v38) is new.
10. **Colours are signed 32-bit ints with an alpha byte.** (proj) default colours read as -10261391 = 0xFF636C71 (mixer insert) and -10721942 = 0xFF5C656A (pattern), i.e. FL's usual blue-greys when read
    as `0xAARRGGBB`; the legacy `midi.CT_ColorT[CT_Sampler]` = 0x6A655C is the same #5C656A in Delphi BBGGRR order. So the stubs' `0x--BBGGRR` looks outdated, the manual says "RGBA", and
    `utils.ColorToRGB(c & 0xFFFFFF)` (which assumes RRGGBB) probably gives (R, G, B). Treat as opaque and round-trip the raw int unchanged; channel colours unverified (check 11.10).

## Sources and trust (details of how things were checked)
Compiled 2026-10-07. Most trusted first:
- **engine**: function names read (read-only scan) from the method tables of the installed `FLEngine_x64.dll`, plus its callback-name table. They match your
  live `dir()` capture in `docs/fl_api_v40_surface.md` one-to-one (all 10 modules), so every "exists in API 40" below is solid.
- **FL files**: `Shared/Python/Lib/midi.py` + `utils.py` (the constants scripts really get at runtime) and the install's `WhatsNew.rtf` (what was added/fixed, in which build).
- **manual** `(m)`: Image-Line online manual `midi_scripting.htm`: signatures, defaults, version column. It documents API up to v45, i.e. newer than your FL.
- **stubs**: fl-studio-api-stubs 37.0.1 (last release Aug 2024, unmaintained): docstrings and `Included since API version N`. Stale in places (Appendix B).
- **UCS**: MaddyGuthridge/Universal-Controller-Script (`src/common/util/api_fixes.py`), written by the stubs' author; used for how focus ids are decoded in practice.

## 1. channels

### 1.1 Index model (read first)
- GLOBAL index = position in the whole rack, `0 .. channelCount(True)-1`. GROUP index = position among only the channels of the channel-rack group (filter tab) that is selected
  in the Channel Rack UI right now. Both 0-based. With the "All" filter they are presumably equal (unverified).
- Default `useGlobalIndex=False` means GROUP index, for: all `channels.*` with an `index` (flag added v33 = FL 2024), all `plugins.*` channel indexes (v26), `channelCount()`.
  Always GLOBAL: `channelNumber()`, `midiNoteOn(indexGlobal, ...)`. Opt-in: `selectedChannel(canBeNone, offset, indexGlobal=True)`.
  Always GROUP, no flag: `mixer.linkChannelToTrack(channel, ...)` ("respecting groups"); `selectAll()/deselectAll()` act on the current group only.
- Converter: `channels.getChannelIndex(groupIndex) -> globalIndex` [v1]. No global->group function exists: loop `i in range(channelCount())` and compare; group order equals global
  order (UCS comment), so the loop may stop once `getChannelIndex(i) > wanted`. (`getGlobalIndex` and `getGridBitWithLength` do not exist; the latter is `getGridBitWithLoop`.)
- Group indexes change when the user switches group tab or moves a channel between groups (`OnRefresh` flag `HW_Dirty_ChannelRackGroup`, 32768). Global indexes change only when channels are
  added/deleted/reordered (`OnDirtyChannel` `CE_New/CE_Delete/CE_Replace`). Neither is an ID: re-enumerate on those events and sanity-check with `getChannelType` + `getChannelName` + plugin name.
- No function reads group names or membership, switches the active group, or adds/removes/moves channels.
- `uGI` coverage. HAS it (default False; `showGraphEditor` default True per stub / False per manual; `rerollLoopStarter*` default True): getChannelName, setChannelName, getChannelColor, setChannelColor,
  isChannelMuted, muteChannel, isChannelSolo, soloChannel, getChannelVolume, setChannelVolume, getChannelPan, setChannelPan, getChannelPitch, setChannelPitch, getChannelType, isChannelSelected,
  selectChannel, selectOneChannel, getChannelMidiInPort, getTargetFxTrack, setTargetFxTrack, getRecEventId, isGridBitAssigned, getGridBit, setGridBit, getGridBitWithLoop, getCurrentStepParam,
  setStepParameterByIndex, showEditor, focusEditor, showCSForm, showGraphEditor, getActivityLevel, quickQuantize, fillEachNSteps (5th positional), rerollLoopStarterLoop/Steps/Sample.
  LACKS it: channelCount (`globalCount`), selectedChannel (`indexGlobal`), channelNumber (global), getChannelIndex (converter), selectAll, deselectAll, midiNoteOn (global), getStepParam, closeGraphEditor(index)
  (unspecified), `patterns.burnLoop/getChannelLoopStyle/setChannelLoop` (unspecified), `mixer.linkChannelToTrack` (group). Every `plugins.*` function has the flag too (v26).
- Out-of-group traps: `selectOneChannel(globalIdxOutsideGroup, True)` deselects everything; `selectChannel(...)` is a no-op there.
- Recipe: `for ch in range(channels.channelCount(True)): channels.getChannelName(ch, useGlobalIndex=True)`; plugin of that channel: `plugins.getPluginName(ch, -1, False, useGlobalIndex=True)`;
  never mix group and global values in one code path. `OnDirtyChannel(index, ...)`: whether `index` is group or global is undocumented (unverified): re-enumerate instead of trusting it.

### 1.2 Properties (`index` first, `uGI` last unless noted; the flag is v33 for all of them)
- `channelCount(globalCount=False) -> int` [v1, param v3]: channels in the current group, or ALL channels (any type) with `True`.
- `selectedChannel(canBeNone=False, offset=0, indexGlobal=False) -> int` [v5]: first selected; `offset=n` = (n+1)-th selected (clamped to the last one); none selected -> 0, or -1 if `canBeNone`.
  `channelNumber(canBeNone=False, offset=0)` [v1] is the same but always global (manual states the -1/0 rule reversed; stubs' version assumed).
- `getChannelName(index, uGI) -> str` / `setChannelName(index, name, uGI)` [v1]. `setChannelName(i, "")` resets to the default plugin/sample name.
- `getChannelColor(index, uGI) -> int` / `setChannelColor(index, color, uGI)` [v1]: opaque int, see caveat 10.
- `isChannelMuted(index, uGI) -> bool` / `muteChannel(index, value=-1, uGI)`: -1 toggle, 1 mute, 0 unmute.
- `isChannelSolo(index, uGI) -> bool` / `soloChannel(index, value=-1, uGI)` [`value` since v30, uGI v33; the stub signature lacks `value`, so a positional 2nd arg is `value`, not uGI].
- `getChannelVolume(index, useDb=0, uGI) -> float` 0..1 (new channels: `midi.ChannelDefaultVolume` = 0.78125); `useDb=1` returns dB.
  `setChannelVolume(index, volume, pickupMode=PIM_None, uGI)`: 0..1.
- `getChannelPan(index, uGI) -> float` -1..1 (0 centre) / `setChannelPan(index, pan, pickupMode=PIM_None, uGI)`.
- `getChannelPitch(index, mode=0, uGI)` [v8]: mode 0 = bend as a fraction of the pitch range (-1..1, may exceed with automation); 1 = offset in CENTS (stubs, tested; the manual says semitones);
  2 = range in semitones ("not guaranteed correct"). `setChannelPitch(index, value, mode=0 (manual: pitchUnit), pickupMode, uGI)`: result clamped to the range; mode 2 is "UTTERLY BROKEN"
  (desyncs the UI), never use it.
- `getChannelType(index, uGI) -> int` [v19]: `CT_Sampler` 0, `CT_Hybrid` 1 (= `CT_TS404`), `CT_GenPlug` 2, `CT_Layer` 3, `CT_AudioClip` 4, `CT_AutoClip` 5.
- `getTargetFxTrack(index, uGI) -> int` mixer track / `setTargetFxTrack(channelIndex, mixerIndex, uGI)` ((m) v28; stubs say v1).
- `getChannelMidiInPort(index, uGI) -> int`: port number, -3 touch keyboard, -2 unassigned, -1 typing keyboard.
- Selection: `isChannelSelected(index, uGI)`, `selectChannel(index, value=-1, uGI)` (-1 toggle / 0 / 1), `selectOneChannel(index, uGI)` [v8], `selectAll()`, `deselectAll()`.
- UI/misc: `getActivityLevel(index, uGI) -> float` [v9]; `quickQuantize(index, startOnly=1, uGI)` [v9]; `showEditor(index, value=-1, uGI)`; `focusEditor(index, uGI)`;
  `showCSForm(index, state=1, uGI)`; `isHighLighted()`; `midiNoteOn(indexGlobal, note, velocity, channel=-1)` (velocity 0 = off, `channel` is ignored).
- Loop Starter NEW40: `rerollLoopStarterLoop/Steps/Sample(index, uGI=True)` ((m) v39; index -1 = all), `patterns.setLoopStarterRootNote(rootNoteNum)`.

### 1.3 Step sequencer
All of these act on the ACTIVE pattern (`patterns.patternNumber()`); the docs never say so, but the only function with a pattern argument is `setStepParameterByIndex`.
- `getGridBit(index, position, uGI) -> bool` / `setGridBit(index, position, value, uGI)` [v1]. `position` = 0-based step along the whole pattern (not just the 16 visible ones).
  Number of steps: docs say `patterns.getPatternLength(pat)` is in BEATS (so steps = beats * 4, 4 steps per beat by default), but (proj) `flsync_channels._steps()` and its fake treat it as STEPS (16 for an
  empty pattern) with a 16-step minimum: unverified, see check 11.12. (proj, assumed) switching on a step past the pattern's end makes the pattern longer. Which callback reports step edits is unverified
  (most likely `OnRefresh` + `HW_Dirty_Patterns`; `OnDirtyChannel` only has New/Delete/Replace/Rename/Select flags).
- `getGridBitWithLoop(index, position, uGI) -> bool` [v1]: the bit after applying the channel's step-sequencer loop (see `patterns.getChannelLoopStyle`).
- `isGridBitAssigned(index, uGI) -> bool` ((m) v30): meaning undocumented ("HELP WANTED" in the stubs).
- `getCurrentStepParam(index, step, param, uGI) -> int` [v1]: the step parameter of channel `index` in the active pattern. Use this one.
- `getStepParam(step, param, offset, startPos, padsStride=16) -> int` [v1]: NO channel argument (the manual lists uGI, the engine's argument list does not). `step` looks like a flattened
  pad index of a hardware grid (changelog 18249 fixed a divide-by-zero, so `padsStride` divides it). Avoid.
- `setStepParameterByIndex(index, patNum, step, param, value, uGI)` [v1]: `patNum` = pattern number, 1-BASED (the stub example passes `patterns.patternNumber()`). Works on any pattern; does not switch
  a step on or off (unverified whether it creates one). Changelog 16027: setting "shift" used to cause audio ducking (fixed).
- `fillEachNSteps(index, interval, offset, velocity, uGI)` NEW40 (active pattern; FL's own remote script calls `channels.fillEachNSteps(chan, stride, offset, velocity, 1)`).
- Step params (`midi.p*`; ranges per stubs): `pPitch` 0 (MIDI note, def 60), `pVelocity` 1 (0..127, def 100), `pRelease` 2 (def 64), `pFinePitch` 3 (0..240, def 120 = 0 cents, +-1200 cents),
  `pPan` 4 (0..128, def 64 centre; one stub docstring says 0..127), `pModX` 5 / `pModY` 6 (stubs: 0..127, def 64; FLP note data uses 0..255 / 128, verify), `pShift` 7 (ticks, 0..PPQ/4),
  `pRepeat` 8 NEW40 (in FL's `midi.py`, not in the stubs; get-support since 24.2.99 beta 5).
- Graph editor UI: `showGraphEditor(temporary, param, step, index, uGI)` (stub default uGI=True, manual False: pass it), `updateGraphEditor()`, `closeGraphEditor(index)`, `isGraphEditorVisible()`.
- Cross-pattern: `patterns.getChannelLoopStyle(pattern, channel)` is the only read (undocumented, "can crash FL on invalid arguments"). Everything else needs `patterns.jumpToPattern`.
```python
def read_active_rows():        # rows of the ACTIVE pattern only; to read pattern n you must first patterns.jumpToPattern(n) (user-visible!)
    pat = patterns.patternNumber()
    steps = max(16, patterns.getPatternLength(pat) * 4)            # docs: beats -> *4; if it is already steps drop the *4 (check 11.12)
    return pat, {ch: "".join("1" if channels.getGridBit(ch, s, useGlobalIndex=True) else "0" for s in range(steps))
                 for ch in range(channels.channelCount(True))}     # audio / automation clip channels (getChannelType 4, 5) have no step row: skip them (their getGridBit result is unverified)
```

### 1.4 REC event ids
- `channels.getRecEventId(index, uGI) -> int`: REC base of the channel (presumably `globalIndex * midi.REC_ItemRange`, 0x10000). Event = base + `midi.REC_Chan_*`: Vol 0, Pan 1, FCut 2, FRes 3, Pitch 4,
  FType 5, PortaTime 6, Mute 7, FXTrack 8, GateTime 9, Crossfade 10, TimeOfs 11, SwingMix 12, SmpOfs 13, StretchTime 14, OfsPan/Vol/Pitch/FCut/FRes 16-20. A hosted plugin's parameter p =
  base + `REC_Chan_Plugin_First` (0x8000) + p (derived from the constants, unverified). `channels.incEventValue(eventId, step, res=1/24)` helps encoders. `channels.processRECEvent` is the deprecated alias.

## 2. mixer
Index: 0 = master, 1.. = inserts, plus the special "Current" track (receives the selected track). Effect slots (`plugIndex`/`slot`): 0..9 (10 per track).

### 2.1 Track list
- `trackCount() -> int` [v1]: manual says "including Master and the Current utility track" (see caveat 9 for 25.2.4). `getTrackCount() -> int` NEW40 ((m) v38): excludes both.
- `getTrackInfo(mode) -> int` [v1]: `TN_Master` 0, `TN_FirstIns` 1, `TN_LastIns` 2, `TN_Sel` 3 (the "Current" track).
- `trackNumber() -> int` first selected track; `setTrackNumber(n, flags=0)`; `isTrackSelected`; `selectTrack` (toggle); `setActiveTrack(index)` [v27] exclusive; `selectAll/deselectAll`.
- Invalid track number: one-time warning, not an exception (changelog 19997: it used to crash, fixed in 25.1).

### 2.2 Track properties (`index` = track)
- `getTrackName(index, maxLen=-1) -> str` / `setTrackName(index, name)` ("" resets to the default "Master"/"Insert N"). `getTrackColor(index)` / `setTrackColor(index, color)`; slot colours
  `getSlotColor(index, slot)` / `setSlotColor(index, slot, color)` [v32].
- `getTrackVolume(index, mode=0) -> float` 0..1, default 0.8 (`midi.TackDefaultVolume`); mode 1 = dB. `setTrackVolume(index, volume, pickupMode=PIM_None)`. (proj: FL stores 1/12800 steps.)
- `getTrackPan(index)` -1..1 / `setTrackPan(index, pan, pickupMode)`. `getTrackStereoSep(index)` -1..1, default 0 [v12] (-1 = "100% centered", +1 = "100% separated") / `setTrackStereoSep(index, sep, pickupMode)`.
- `isTrackMuted(index)` [v2] / `muteTrack(index, value=-1)` (-1 toggle, 0/1 explicit; `value` is v30 per manual). (proj: `isTrackMuted` shows the change only after FL's next refresh.)
  `isTrackEnabled/enableTrack` behave like not-muted/toggle-mute (duplicates). `isTrackMuteLock(index)` [v13].
- `isTrackSolo(index)` / `soloTrack(index, value=-1, mode=-1)` (mode 1 +sources, 2 +sends, 3 both, 4 only this). `isTrackArmed/armTrack(index)` (toggle).
- `isTrackSlotsEnabled(index)` [v19] / `enableTrackSlots(index, value=-1)` (the "enable effects slots" button; stubs: default False; since 25.1.99 beta 3 default toggles -> always pass `value`).
  Same pattern for `isTrackRevPolarity/revTrackPolarity` and `isTrackSwapChannels/swapTrackChannels`.
- `getTrackDockSide(index)` [v13]: 0 left, 1 centre, 2 right. `getTrackPeaks(index, mode)`: 0 L, 1 R, 2 max; 1.0 = 0 dB. `getLastPeakVol(section)` [v9].

### 2.3 Routing
- `setRouteTo(index, destIndex, value, updateUI=False)`: create/remove the send index->destIndex; call `afterRoutingChanged()` once after a batch (or pass `updateUI=True`, 'updateUI' since v36).
  `getRouteSendActive(index, destIndex) -> bool` [v1].
- `setRouteToLevel(index, destIndex, level)` / `getRouteToLevel(index, destIndex) -> float` [v36]: normalized 0..1 ("default volume 0.8"); the route must already exist.
- `linkChannelToTrack(channel, track, select=False)` [v23] (channel = GROUP index!). `channels.setTargetFxTrack(.., uGI)` is the global-capable alternative. `linkTrackToChannel(mode)` unclear.

### 2.4 Built-in EQ [v35]
- `getEqBandCount() -> int` (3 today; use it). `getEqGain(index, band, mode=0)` and `getEqFrequency(index, band, mode=0)`: normalized 0..1, `mode=1` returns dB / Hz.
  `getEqBandwidth(index, band)`: normalized only. `setEqGain/setEqFrequency/setEqBandwidth(index, band, value)`: normalized 0..1 only.

### 2.5 Effect slots
- `isTrackPluginValid(index, plugIndex) -> bool` [v1]. `getTrackPluginId(index, plugIndex) -> int` = `((index << 6) + plugIndex) << 16` (REC / form id).
- `getPluginMixLevel(index, plugIndex) -> float` 0..1 / `setPluginMixLevel(index, plugIndex, value, pickupMode=PIM_None)` NEW40 ((m) v37). Returned -1..1 until 24.2.1. (proj: read-back lags.)
- `getPluginMuteState(index, plugIndex)` / `setPluginMuteState(index, plugIndex, value)` NEW40, undocumented (changelog 20982). (proj) reads 1 = on (also for empty slots); can switch a slot OFF but never back ON.
- `isTrackAutomationEnabled(index, plugIndex)`; `focusEditor(index, plugIndex)` [v25]; `getActiveEffectIndex() -> (index, plugIndex) | None` [v25] (focused effect editor).
- REC alternative (derived from FL's `midi.py`, unverified): `mixer.getTrackPluginId(t, s) + midi.REC_Plug_Mute / REC_Plug_MixLevel`. FL 2025 moved `REC_Plug_First` to 0x70000000, so such ids for
  tracks >= 64 exceed int32 (arithmetic, effect untested): prefer the `mixer.*` functions.
- Other: `getSongStepPos()`, `getSongTickPos(mode=ST_Int)`, `getRecPPS()`, `getCurrentTempo/setCurrentTempo` (section 8). Mixer event helpers (`getEventValue`, `remoteFindEventValue`, `getEventIDName`,
  `getEventIDValueString`, `getAutoSmoothEventValue`, `automateEvent`) are undocumented ("HELP WANTED").

## 3. plugins
All [v8]; `uGI` since v26. Index convention (module docstring + manual):
- (a) Generator on the channel rack: `index` = channel index, `slotIndex = -1` (default). `index` is the GROUP index unless `useGlobalIndex=True` ("the grouped index is used, rather than a global index").
- (b) Effect in a mixer slot: `index` = mixer track (0 = master), `slotIndex` = 0..9; `uGI` has no meaning there. Tuple-unpacking (`plugins.isValid(*loc)`) is unreliable together with `uGI`.
- Samplers / audio clips / automation clips are not plugins (`isValid` False). Call `isValid` first: the engine has an error string "Plugin not valid" for invalid locations (raised vs printed unverified).
- `isValid(index, slotIndex=-1, uGI) -> bool`; `getPluginName(index, slotIndex=-1, userName=False, uGI) -> str` ([v12] userName: the user's name for the slot, falls back to the original).
- `getParamCount(index, slotIndex=-1, uGI) -> int`; `getParamName(paramIndex, index, slotIndex=-1, uGI) -> str`; `getParamValue(paramIndex, index, slotIndex=-1, uGI) -> float` normalized 0..1.
- `setParamValue(value, paramIndex, index, slotIndex=-1, pickupMode=0, uGI)`: `value` 0..1 comes FIRST (its keyword is `paramValue`); pickupMode 0 none, 1 always, 2 follow FL's global option: use 0 for remote writes.
  No flags for undo / last-tweaked / automation side effects (unknown what it does there).
- `getParamValueString(paramIndex, index, slotIndex=-1, uGI) -> str`: display text such as "-3.2 dB"; only some FL plugins support it. (The stub wrongly puts `pickupMode` before `uGI`.)
- `getColor(index, slotIndex=-1, flag=GC_BackgroundColor, paramIndex=0, uGI)` [v12] (`GC_Semitone` 1 = note/pad colour); `getName(index, slotIndex=-1, flag=FPN_Param, paramIndex=0, uGI)` [v13] with
  FPN_Param 0 / ParamValue 1 / Semitone 2 / Patch 3 / VoiceLevel 4 / VoiceLevelHint 5 / Preset 6 (paramIndex = preset) / OutCtrl 7 / VoiceColor 8 / OutVoice 9;
  `getPadInfo(chanIndex, slotIndex=-1, paramOption=0, paramIndex=-1, uGI)` [v19] (FPC only); `getPresetCount` [v15]; `nextPreset/prevPreset` [v10].
- Do NOT exist: `getParamDefaultValue`, parameter min/max/type, "is automated", any change notification.

### Parameter counting
- FL-native plugins: `getParamCount` is their own count (expected: all parameters named; unverified).
- VST/VST3 through FL's wrapper: 4240 = 4096 + 128 + 16. Indices 0..4095 are the plugin's slots: unused slots still answer, with name `""`. 4096..4223 = MIDI CC 0..127 (CC = idx - 4096) and
  4224..4239 = channel aftertouch for MIDI channels 1..16 (idx - 4224): controller inputs, not plugin state, skip them for state sync.
- Real parameters = `idx < 4096 and getParamName(idx, ...) != ""` (a few real ones with an empty name are indistinguishable). Scan once per plugin instance (4096 `getParamName` calls), cache the index
  list keyed by (location, `getPluginName`), refresh on `OnDirtyChannel` / slot changes / project load, then poll only that list (continuously only for the focused plugin).
```python
def real_params(index, slot=-1):     # channel plugin: slot=-1 and index = GLOBAL channel index; effect: index = track, slot = 0..9
    if not plugins.isValid(index, slot, useGlobalIndex=True):
        return []
    n = min(plugins.getParamCount(index, slot, useGlobalIndex=True), 4096)
    return [p for p in range(n) if plugins.getParamName(p, index, slot, useGlobalIndex=True)]
```

## 4. ui
- `ui.getFocused(index) -> bool` [v1]: `widMixer` 0, `widChannelRack` 1, `widPlaylist` 2, `widPianoRoll` 3, `widBrowser` 4, `widPlugin` 5 (any plugin window; only valid here), `widPluginEffect` 6,
  `widPluginGenerator` 7. `getVisible(index)`, `showWindow(index)`, `hideWindow(index)` [v5], `setFocused(index)` [v2] (0..4 only; open plugin windows with `mixer.focusEditor(track, slot)` /
  `channels.focusEditor(ch, uGI)`). `widPluginPicker` 8 exists only in newer builds.
- `ui.getFocusedFormID() -> int` [v13]: generator window -> channel index; effect window -> mixer plugin id `((track << 6) + slot) << 16` (= track * 4194304 + slot * 65536); mixer / channel rack /
  playlist / piano roll / browser -> that window's `wid*` value (0..4); -1 for an invalid plugin (script output, settings windows). Slot 0 of track 0 gives 0 and channel 0..4 give 0..4, so ids are
  ambiguous without `getFocused`. There is no `getFocusedFormIndex`.
```python
def focused_plugin():          # ("fx", track, slot) | ("gen", global_channel) | None
    if ui.getFocused(midi.widPluginEffect):                 # 6
        fid = ui.getFocusedFormID()
        return ("fx", fid >> 22, (fid >> 16) & 0x3F)
    if ui.getFocused(midi.widPluginGenerator):              # 7
        fid, want = ui.getFocusedFormID(), ui.getFocusedPluginName()          # fid -1 = invalid
        # UCS treats fid as GROUP-relative, FLSync's own code tries it as global first: try both, trust the plugin name
        for ch in (fid, channels.getChannelIndex(fid) if 0 <= fid < channels.channelCount() else -1):
            if 0 <= ch < channels.channelCount(True) and plugins.isValid(ch, -1, useGlobalIndex=True) and want in (
                    plugins.getPluginName(ch, -1, False, useGlobalIndex=True), plugins.getPluginName(ch, -1, True, useGlobalIndex=True)):
                return ("gen", ch)
    return None
```
  `ui.getFocusedPluginName()` [v5] is the plugin's ORIGINAL name (`""` if the focused window is not a plugin); the check above also accepts the user's slot name because FLSync does. It cannot separate two channels that host
  the same plugin: then fall back to `channels.selectedChannel(True, 0, True)` (opening a plugin window presumably selects its channel). `ui.getFocusedFormCaption()` is the window title, not the plugin name.
  Re-evaluate on `OnRefresh` flag `HW_Dirty_FocusedWindow` (32; likely, unverified).
- Hint bar: `ui.getHintMsg() -> str` / `ui.setHintMsg(msg)` [v1] (current text of FL's hint panel). While the user tweaks a control FL shows a description there (expected, unverified: parameter name + value;
  localised, not parseable), and per the manual `OnSendTempMsg(message, duration)` is "called when a hint message is sent to the controller display": at best a crude "something was tweaked" trace.
- "Last tweaked": `device.linkToLastTweaked(controlIndex, channel, global_link=False, eventId=REC_None) -> 0/1/2` [v21] (0 linked, 1 nothing tweaked yet, 2 control in use) creates a link, it does not
  return the parameter. `midi.REC_LastTweakedFirst` (-32) / `REC_LastTweakedLast` (-31) are special REC ids ("last-tweaked parameter?", undocumented); untested idea: `device.getLinkedParamName(midi.REC_LastTweakedFirst)`
  / `getLinkedValueString(...)` might name it. Flag `REC_NoLastTweaked` stops a `processRECEvent` write from becoming the last-tweaked parameter.
- Other: `ui.getVersion(mode=4)` (0 major, 1 minor, 2 release, 3 build -> int; 4-6 -> str; `general.getVersion()` is the API version), `ui.getProgTitle()`, `ui.isClosing()`.

## 5. patterns
Numbering is 1-BASED: 1..`patternMax()` (999). Every `index` below is the pattern number.
- `patternNumber() -> int` [v1]: the ACTIVE pattern (where steps and notes go; the most recently selected one, falling back to the first selected). So `selectPattern(n, 1)` probably also makes `n` active,
  without `jumpToPattern`'s scrolling and creation (unverified; deselect afterwards to hand the previous one back).
- `patternCount() -> int`: only the patterns changed from default, NOT a max index and not necessarily contiguous. Enumerate with
  `for i in range(1, patterns.patternMax() + 1): if not patterns.isPatternDefault(i)` (`isPatternDefault` [v23]).
- `getPatternName(index)` / `setPatternName(index, name)` ("" resets to the default "Pattern N"); `getPatternColor` / `setPatternColor` (opaque int, see caveat 10); `getPatternLength(index) -> int` [v1]: docs say
  BEATS, (proj) FLSync treats it as STEPS: unverified (check 11.12). `setPatternLength` / `incrementPatternLength` are documented for API 39 but ABSENT from 25.2.4 (>40).
- `jumpToPattern(index)` [v1]: scrolls the picker, activates and selects it; creates the pattern if it does not exist (do not jump to odd numbers). `selectPattern(index, value=-1, preview=False)`,
  `isPatternSelected`, `selectAll/deselectAll` [v2]. `findFirstNextEmptyPat(flags, x=-1, y=-1)` (`FFNEP_FindFirst` 0, `FFNEP_DontPromptName` 2), `clonePattern(index=-1)` [v25] (closes the piano roll),
  `movePattern(direction=1)` NEW40 ((m) v39), `burnLoop(index, storeUndo=1, updateUi=1)` [v9]. Pattern groups [v28]: `getActivePatternGroup()` (-1 = All), `getPatternGroupCount()`,
  `getPatternGroupName(index)`, `getPatternsInGroup(index) -> tuple`.
- Undocumented: `getChannelLoopStyle(pattern, channel)` (pattern 1-based, channel 0-based; unclear group/global), `setChannelLoop(channel, loopPoint)` (active pattern; "can crash FL on invalid arguments").
- Change signal: only `OnRefresh` flag `HW_Dirty_Patterns` (1024: "selected pattern changed or pattern itself changed", no index). Poll `patternNumber()`; changelog 15228 fixed missing notifications while the picker is hidden.

## 6. playlist
Tracks are 1-BASED: `1 .. trackCount()` inclusive (500 tracks in current FL; empty ones included).
- `trackCount() -> int`; `getTrackName(index)` (unnamed: "Track N"; the stub says N = index + 1 which contradicts 1-based indexing, FLSync assumes N = index: check 11.11) / `setTrackName(index, name)` ("" resets); `getTrackColor(index)` / `setTrackColor(index, color)` (opaque int; manual: RGBA).
- `isTrackMuted(index)` / `muteTrack(index, value=-1, inGroup=0)` (`value` v30, `inGroup` v33; stub lacks `inGroup`): -1 toggle, 1 mute, 0 unmute. `isTrackMuteLock` / `muteTrackLock` (toggle) [v2].
- `isTrackSolo(index)` / `soloTrack(index, value=-1, inGroup=0)`. `isTrackSelected/selectTrack/selectAll/deselectAll` [v12]. `getTrackActivityLevel` (0.0 or 0.5), `getTrackActivityLevelVis` (0..1).
- `selectTool(index)` NEW40 (v40): selects the playlist / piano-roll tool. Nothing exists for clips or items on the playlist (only performance-mode live-clip functions), track add/remove, or markers beyond `arrangement.*`.

## 7. transport
- `isPlaying() -> bool`, `start()` (stubs: play/PAUSE toggle; manual: "start playback": check `isPlaying()` first), `stop()`, `isRecording() -> bool`, `record()` (toggle).
  (proj) `isPlaying()` can lag a script's own `start()/stop()` by a refresh: remember what you asked for for about a second; `stop()` returns to where playback started.
- `getLoopMode() -> int` 0 pattern / 1 song (`midi.SM_Pat`, `SM_Song`); `setLoopMode()` only toggles.
- `getSongPos(mode=-1)` / `setSongPos(position, mode=-1)` [v1, mode v3/v4]: -1 = fractional float 0..1 of the song (song mode) or pattern (pattern mode); `SONGLENGTH_MS` 0, `_S` 1, `_ABSTICKS` 2
  (ticks from the start; PPQ = `general.getRecPPQ()`, default 96, 4 steps per beat); `_BARS` 3 (1-based), `_STEPS` 4, `_TICKS` 5 are B:S:T components and get-only.
- `getSongLength(mode)` [v3]: same modes, full song only (pattern length: `patterns.getPatternLength`). `getSongPosHint() -> "B:S:T"`.
- `globalTransport(command, value, pmeflags=PME_System, flags=GT_All) -> int` [v1]: generic FPT_ dispatcher; returns a `GT_*` flag (`GT_Cannot` -1, `GT_Plugin` 1, `GT_Form` 2, `GT_Menu` 4, `GT_Global` 8, `GT_All` 15).
  `value` is command specific (jog = delta, button = nonzero). Relevant `FPT_`: Play 10, Stop 11, Record 12, Loop 15, Undo 20, Save 92, TempoJog 105 ("0.1 BPM increments"), TapTempo 106,
  NudgeMinus/Plus 107/108, Metronome 110, StepEdit 114, CountDown 115, ShuffleJog 122 ("main shuffle, increments of 1"). Prefer the dedicated functions.
- Misc: `rewind / fastForward / continuousMove / continuousMovePos` are hold-style with `SS_Stop` 0 / `SS_StartStep` 1 / `SS_Start` 2; `setPlaybackSpeed(0.25..4.0)`; `markerJumpJog`, `markerSelJog`;
  `getHWBeatLEDState` (stubs: always 0).

## 8. Song settings
- **Tempo (BPM).** Get: `mixer.getCurrentTempo(asInt=False)` [v1]. Documented as float BPM (int with `asInt`), but (proj, observed live on 2025.2.4) `getCurrentTempo()` returned BPM*1000 and `getCurrentTempo(True)`
  returned BPM, the reverse: normalise with `raw / 1000 if raw > 1000 else raw` (FL's tempo range is 10..522 BPM, so the two kinds cannot overlap).
  Set: `general.processRECEvent(midi.REC_Tempo, round(bpm * 1000), midi.REC_Control | midi.REC_UpdateControl)` (the stubs' documented example and what FLSync uses; `REC_Tempo` = 0x40000005, stored as 1000*BPM).
  `mixer.setCurrentTempo(tempo, asInt=0)` NEW40 ((m) v38; float accepted since 25.2.4 RC1, changelog 21390) also exists but its units are as unclear as the getter's: test before using it.
  Relative: `transport.globalTransport(midi.FPT_TempoJog, delta)`. No tempo-change callback: poll in `OnIdle`. Writes may raise the unsafe-time `TypeError` (check `general.safeToEdit()`).
- **Time signature.** Set: `general.setNumerator(n)`, `general.setDenominator(d)`, timebase `general.setRecPPQ(ppq)` NEW40 ((m) v38; changelog 21347, 25.2.4 RC1); valid ranges undocumented
  (expect clamping/rejection; changing PPQ probably rescales existing content, so write it only on a real change). Get: `general.getRecPPQ()` [v8], `general.getRecPPB()` [v1] = "timebase * numerator" and ignores
  time-signature markers, so `numerator = getRecPPB() // getRecPPQ()`. `getNumerator/getDenominator` do not exist: the DENOMINATOR cannot be read in API 40 (the piano-roll scripting module `flpianoroll`
  has `score.numerator/denominator` but is not available to controller scripts). Time-signature markers on the playlist: no API.
- **Swing** (the channel-rack SWING knob = FL's "main shuffle"): no function in API 40. `general.processRECEvent(midi.REC_MainShuffle, v, flags)` (id = 0x40000001): with `REC_FromMIDI` in `flags`, `v` is
  0..`midi.FromMIDI_Max` (2**30) normalized; without it `v` is in native units (scale unknown). Read with `REC_GetValue` (unverified). Relative: `FPT_ShuffleJog`. >40: `channels.getShuffle() -> float` /
  `setShuffle(value)` normalized (API 42). Per-channel swing mix: `REC_Chan_SwingMix`.
- **Master pitch / volume:** `midi.REC_MainPitch` (0x40000002) / `REC_MainVol` (0x40000000) via `processRECEvent`, same caveats. No functions.
- **Project info** [v38] NEW40: `general.getProjectTitle()`, `getProjectAuthor()`, `getProjectGenre()` -> str. Also `general.getUseMetronome()`, `getPrecount()`, `ui.isMetronomeEnabled()`, `ui.isLoopRecEnabled()`.

## 9. Script callbacks
Names FL 25.2.4 recognises: OnMidiIn, OnMidiOutMsg, OnMidiMsg, OnNoteOn, OnNoteOff, OnControlChange, OnProgramChange, OnPitchBend, OnKeyPressure, OnChannelPressure, OnSysEx, OnInit, OnDeInit, OnIdle,
OnRefresh, OnDoFullRefresh, OnUpdateBeatIndicator, OnDisplayZone, OnUpdateLiveMode, OnDirtyMixerTrack, OnUpdateMeters, OnWaitingForInput, OnSendTempMsg, OnFirstConnect, OnDirtyChannel, OnProjectLoad, OnInitAll.
- `OnInit()` / `OnDeInit()` (may be called more than once per script lifetime). `OnInitAll()` NEW40: undocumented (changelog 19777, May 2025); presumably runs once all scripts finished `OnInit` (unverified).
- `OnIdle()`: roughly every 20 ms; keep it tiny. (proj: FL does not always call it; the heartbeat drives ticks too.)
- `OnMidiIn(msg)`: first chance; `msg.handled = True` stops FL from processing it (use for filtering only). `OnMidiMsg(msg)` second chance, then `OnSysEx`, `OnNoteOn/Off`, `OnControlChange`, ...
  Fields: handled, timestamp, status, data1, data2, port, note, velocity, pressure, progNum, controlNum, controlVal, pitchBend, sysex, isIncrement, res, inEv, outEv, midiId, midiChan, midiChanEx, senderId, pmeFlags.
- `OnRefresh(flags)`: bit mask, FL's `midi.py` values and meaning (stubs / FL comments):
  `HW_Dirty_Mixer_Sel` 1 mixer selection; `_Mixer_Display` 2 mixer display; `_Mixer_Controls` 4 mixer controls; `_RemoteLinks` 16 links added/removed; `_FocusedWindow` 32 focused window / selected channel;
  `_Performance` 64 performance layout; `_LEDs` 256 transport/LED state (play, stop, record, active window); `_RemoteLinkValues` 512 linked control value; `_Patterns` 1024 selected pattern or pattern itself;
  `_Tracks` 2048 "track changes"; `_ControlValues` 4096 plugin control values; `_Colors` 8192; `_Names` 16384; `_ChannelRackGroup` 32768 group tabs changed; `HW_ChannelEvent` 65536 (pairs with OnDirtyChannel).
  >40 (not in your midi.py): `HW_Dirty_Undo` 131072, `_ChannelRack` 262144, `_GraphEditor` 524288. `OnDoFullRefresh()`: same, update everything.
- `OnDirtyChannel(index, flag)` [v16], TWO args (the stub shows one): `index` = changed channel or -1 = all (group or global: unverified); `flag`: `CE_New` 0, `CE_Delete` 1, `CE_Replace` 2, `CE_Rename` 3, `CE_Select` 4.
  Collect here, act on the following `OnRefresh` with `HW_ChannelEvent`. No flag exists for volume/pan/mute/colour/step edits.
- `OnDirtyMixerTrack(index)` [v1]: changed track or -1 = all; act on the `OnRefresh` with `HW_Dirty_Mixer_Controls`. `device.hardwareRefreshMixerTrack(index)` re-dispatches it (-1 = all).
  (proj: FL does not say which control moved; the selected track is the best guess.)
- `OnUpdateBeatIndicator(value)`: 0 off, 1 bar, 2 beat. `OnProjectLoad(status)` [v16]: `PL_Start` 0, `PL_LoadOk` 100, `PL_LoadError` 101. `OnSendTempMsg(message, duration_ms)`: hint-bar text.
  `OnUpdateMeters()` needs `device.setHasMeters()` in `OnInit`. Also `OnFirstConnect` [v17], `OnWaitingForInput`, `OnDisplayZone`, `OnUpdateLiveMode(lastTrack)`.
- Pattern changes: only `OnRefresh` + `HW_Dirty_Patterns` (no index). Plugin parameter changes: only `OnRefresh` + `HW_Dirty_ControlValues` (no plugin, no parameter) and the `OnSendTempMsg` hint text.
  Neither `OnDirtyChannel` nor `OnDirtyMixerTrack` is documented to cover plugin parameters.

## 10. general
- `general.processRECEvent(eventId, value, flags) -> int` [v7]: reads/writes any automatable control by REC event id. `value` is in the control's native integer units, or 0..`midi.FromMIDI_Max` (2**30)
  normalized when `flags` contains `REC_FromMIDI`. Return value officially "unknown"; with `REC_GetValue` it should return the current native value, `midi.REC_InvalidID` (MaxInt) for a bad id (unverified).
  "Providing an invalid value can lead to very strange behavior and sometimes crashes". Try the dedicated API first.
- Flags (FL's runtime `midi.py`): `REC_UpdateValue` 1, `REC_GetValue` 2, `REC_ShowHint` 4, `REC_UpdatePlugLabel` 8, `REC_UpdateControl` 16, `REC_FromMIDI` 32, `REC_Store` 64, `REC_SetChanged` 128,
  `REC_SetTouched` 256, `REC_Init` 512, `REC_NoLink` 1024, `REC_InternalCtrl` 2048, `REC_PlugReserved` 4096, `REC_Smoothed` 8192, `REC_NoLastTweaked` 16384, `REC_NoSaveUndo` 32768.
  Combos: `REC_Control` 973 (no UpdateControl: the knob does not move), `REC_Controller` 981, `REC_MIDIController` 1013 (= 981 | REC_FromMIDI), `REC_Visual` = GetValue|UpdateControl|UpdatePlugLabel.
  For applying a remote value quietly, a plausible set is `REC_UpdateValue | REC_UpdateControl | REC_SetChanged | REC_NoLastTweaked | REC_NoSaveUndo` (+ `REC_FromMIDI` when normalized); untested.
- Event-id helpers: `channels.getRecEventId(index, uGI)` (section 1.4); `mixer.getTrackPluginId(track, slot)`; none in `plugins`. Track property = `mixer.getTrackPluginId(t, 0) + midi.REC_Mixer_Vol`
  (also `REC_Mixer_Pan`, `_SS`, `_EQ_Gain`, `_EQ_Freq`, `_EQ_Q`; derived from FL's constants, unverified; the stubs' mixer constants are outdated). Name/value of any id: `device.getLinkedParamName(id)` [v10], `device.getLinkedValueString(id)` [v10],
  `device.getLinkedValue(id)` (normalized 0..1, -1 if none), `device.findEventID(controlId)`, `device.getLinkedInfo(id)`, `device.getLinkedChannel(id)` [v27].
- `general.saveUndo(undoName, flags, update=True)` [v1]: adds an undo point; `flags` = `UF_None` 0, `UF_EE` 1, `UF_PR` 2 (piano roll / step sequencer), `UF_PL` 4, `UF_Knob` 32 (`UF_KNOB`), `UF_AudioRec` 256,
  `UF_AutoClip` 512, `UF_PRMarker` 1024, `UF_PLMarker` 2048, `UF_Plugin` 4096, `UF_SSLooping` 8192, `UF_Reset` 65536 (documented, but not defined in FL's `midi.py`: pass the literal).
  `undo()` toggles undo/redo; `undoUp/undoDown/undoUpDown(n)`; `getUndoHistory*`/`setUndoHistory*` [v1].
- `general.safeToEdit() -> bool` [v29]: True when setters are safe (unsafe calls raise `TypeError("Operation unsafe at current time")`; `FlMidiMsg.pmeFlags` has `PME_System` 2 / `PME_System_Safe` 4).
- `general.getChangedFlag() -> int` [v1]: 0 clean, 1 changed since save, 2 changed but not since the last autosave. `general.getVersion() -> int` = API version (40).
- `general.getRecPPB()`, `getRecPPQ()`, `getUseMetronome()`, `getPrecount()`, `dumpScoreLog(time, silent=False)` [v15], `clearLog()`.

## 11. Live checks that would settle the "unverified" items (each a one-minute test through your log channel)
1. Group vs global: make two channel groups, select the second tab, focus a plugin window in it; log `ui.getFocusedFormID()`, `channels.getChannelIndex(fid)`, and the raw `index` that `OnDirtyChannel` gets after renaming that channel.
2. Tempo scale: re-confirm the (proj) finding by logging `getCurrentTempo()`, `getCurrentTempo(True)` and `general.processRECEvent(midi.REC_Tempo, 0, midi.REC_GetValue)`; then see what `setCurrentTempo(140.5)` and
   `setCurrentTempo(140500, True)` do before trusting `setCurrentTempo`.
3. Swing / master pitch: log `processRECEvent(midi.REC_MainShuffle, 0, midi.REC_GetValue)` with the knob at min / centre / max (same for `REC_MainPitch` at +-12 st); then test writes with and without `REC_FromMIDI`.
4. Denominator: log `getRecPPB()` and `getRecPPQ()` after `setNumerator(6)` + `setDenominator(8)` (and 6/4): if PPB changes with the denominator it is derivable, otherwise it is not.
5. Last tweaked: after wiggling a plugin knob, log `device.getLinkedParamName(midi.REC_LastTweakedFirst)` and `getLinkedValueString(midi.REC_LastTweakedFirst)`.
6. Dirty flags: log `OnRefresh` flags in binary while you toggle a step, move a plugin knob, change tempo, rename/recolour a channel, move a mixer-slot mix knob, switch channel-rack tab.
7. Active-pattern assumption: toggle a step in pattern 2, switch to pattern 1, compare `getGridBit` on both (it should reflect the ACTIVE pattern only).
8. Parameters: for one FL-native plugin and one VST log `getParamCount` and how many `getParamName` are non-empty (expect native: all; VST: 4240 vs a few hundred).
9. Toggles: call `transport.start()` while playing (pause or no-op?) and `soloChannel(i, True)` vs `soloChannel(i, 1, useGlobalIndex=True)` to confirm the `value` slot.
10. Colours: log the raw `getChannelColor` of a default sampler channel (0xFF5C656A-style = ARGB, 0x6A655C-style = BBGGRR) and set `0xFFFF0000` on a test channel (red = ARGB, blue = ABGR).
11. Playlist bounds: log `playlist.getTrackName(1)`, `getTrackName(playlist.trackCount())` and `getTrackName(playlist.trackCount() + 1)` to confirm 1..N inclusive and the default "Track N" rule.
12. Pattern length unit: log `patterns.getPatternLength(n)` for an empty pattern and for one stretched to 2 bars (4 and 8 = beats; 16 and 32 = steps), and the number of steps `getGridBit` can address.

## Appendix A. API 40 vs the stubs (v37)
Exist in 25.2.4, missing from the stubs (NEW40):
- channels: `fillEachNSteps`, `rerollLoopStarterLoop/Steps/Sample`. mixer: `getTrackCount`, `setCurrentTempo`, `getPluginMixLevel/setPluginMixLevel` (v37), `getPluginMuteState/setPluginMuteState`.
- general: `setRecPPQ`, `setNumerator`, `setDenominator`, `getProjectTitle/Author/Genre`. patterns: `movePattern`, `setLoopStarterRootNote`. playlist: `selectTool`.
- arrangement: `selectionIsActive`, `selectionSet`, `selectionClear`. device: `releaseCC`, `flrMidiNote`, `flrMidiCC`. Callback `OnInitAll`. `midi.py`: `pRepeat` 8, `widPlugin` 5, `SS_*`, `EETool_*`.
- Stub names that exist in the stubs but are NOT real constants in FL's `midi.py`: `Snap_FourthBar/HalfBar/SixthBar/ThirdBar`.
- When they arrived (WhatsNew.rtf), for FL builds older than yours: `get/setPluginMixLevel` 2024.1.2 (Sep 2024); `getTrackCount`, `getProject*`, `arrangement.selection*`, `pRepeat` in step reads, `OnInitAll` 24.2.99 betas = FL 2025.1
  (May 2025); `fillEachNSteps` 25.1.99 b1 (Oct 2025); `get/setPluginMuteState`, `device.releaseCC`, toggling `enableTrackSlots/revTrackPolarity/swapTrackChannels` 25.1.99 b3 (Oct 2025); `movePattern` 25.2.2 (Dec 2025);
  `setCurrentTempo` (float since RC1), `setRecPPQ`, `setNumerator`, `selectTool` 25.2.4 RC1 (Jan 2026; `setDenominator` is not named in the changelog). The manual labels the tempo/time-signature setters "v38" although they are newer
  than `movePattern` ("v39"): API version numbers are not a safe availability test, use `hasattr(module, "name")`.

## Appendix B. Stub / manual / engine disagreements (use the right-hand side)
- `soloChannel(index, value=-1, uGI)`; `getChannelVolume(index, useDb, uGI)`; `setChannelPitch(index, value, pitchUnit, pickupMode, uGI)`; `showGraphEditor` uGI default is False in the manual (stub True).
- `getStepParam`: no channel argument and (engine) no uGI. `setTargetFxTrack` v28, `isGridBitAssigned` v30 (stubs: v1). `channelNumber` no-selection rule: stubs 0 / -1 with `canBeNone`; manual reversed.
- `plugins.getParamValueString(paramIndex, index, slotIndex, uGI)`; `plugins.getColor(index, slotIndex, flag, paramIndex, uGI)`; `setParamValue(paramValue, paramIndex, index, slotIndex, pickupMode, uGI)`.
- `mixer.getTrackName(index, maxLen=-1)`; `enableTrackSlots/revTrackPolarity/swapTrackChannels(index, value=-1)` toggle by default since 25.1.99 beta 3 (stubs: `value=False`, "known issue").
- `playlist.muteTrack(index, value=-1, inGroup=0)`, `playlist.soloTrack(index, value=-1, inGroup=0)`; `patterns.clonePattern(index=-1)` (stub: None).
- `OnDirtyChannel(index, flag)`. `getChannelPitch` mode 1: cents (stubs, tested) vs semitones (manual).
- `midi`: stubs' `REC_Plug_*`/`REC_Mixer_*` use old values (`REC_Plug_First` 0x20000000, FL: 0x70000000; `REC_Mixer_Send_First`, `REC_Mixer_Vol` also moved) and are not even re-exported by the stubs' `midi`;
  `REC_Control` 973 / `REC_Controller` 981 / `REC_MIDIController` 1013 in FL vs 973 / 973 / 1005 in the stubs (stubs lack `REC_UpdateControl` in `REC_MIDIController`).

## Appendix C. Newer than API 40 (documented in the manual, absent from 25.2.4; feature-detect with `hasattr`)
- `ui.showPicker(mode, category=-1)` v41; `channels.getShuffle()` / `setShuffle(value)` v42; `patterns.clonePattern(index, destIndex)` v43 (the `index=-1` default exists already), `clearPattern(index=-1)` v44,
  `duplicatePatternData(index)` v45; `patterns.setPatternLength(index, length)` and `incrementPatternLength(index, offset)` (manual: v39, but missing from the 25.2.4 engine).
- Constants: `midi.widPluginPicker` 8, `HW_Dirty_Undo` 131072, `HW_Dirty_ChannelRack` 262144, `HW_Dirty_GraphEditor` 524288.
