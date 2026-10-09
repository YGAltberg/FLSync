# FL Studio 2025.2.4 (API 40): functions that actually exist

Captured live from the user's FL with dir() on each module (sent through the bridge's
log channel), 2026-10-07. The API stubs are version 37, so check here that a function
really exists in this FL before relying on it.

## channels

`channelCount`, `channelNumber`, `closeGraphEditor`, `deselectAll`, `fillEachNSteps`, `focusEditor`, `getActivityLevel`, `getChannelColor`, `getChannelIndex`, `getChannelMidiInPort`, `getChannelName`, `getChannelPan`, `getChannelPitch`, `getChannelType`, `getChannelVolume`, `getCurrentStepParam`, `getGridBit`, `getGridBitWithLoop`, `getRecEventId`, `getStepParam`, `getTargetFxTrack`, `incEventValue`, `isChannelMuted`, `isChannelSelected`, `isChannelSolo`, `isGraphEditorVisible`, `isGridBitAssigned`, `isHighLighted`, `midiNoteOn`, `muteChannel`, `processRECEvent`, `quickQuantize`, `rerollLoopStarterLoop`, `rerollLoopStarterSample`, `rerollLoopStarterSteps`, `selectAll`, `selectChannel`, `selectOneChannel`, `selectedChannel`, `setChannelColor`, `setChannelName`, `setChannelPan`, `setChannelPitch`, `setChannelVolume`, `setGridBit`, `setStepParameterByIndex`, `setTargetFxTrack`, `showCSForm`, `showEditor`, `showGraphEditor`, `soloChannel`, `updateGraphEditor`

## mixer

`afterRoutingChanged`, `armTrack`, `automateEvent`, `deselectAll`, `enableTrack`, `enableTrackSlots`, `focusEditor`, `getActiveEffectIndex`, `getAutoSmoothEventValue`, `getCurrentTempo`, `getEqBandCount`, `getEqBandwidth`, `getEqFrequency`, `getEqGain`, `getEventIDName`, `getEventIDValueString`, `getEventValue`, `getLastPeakVol`, `getPluginMixLevel`, `getPluginMuteState`, `getRecPPS`, `getRouteSendActive`, `getRouteToLevel`, `getSlotColor`, `getSongStepPos`, `getSongTickPos`, `getTrackColor`, `getTrackCount`, `getTrackDockSide`, `getTrackInfo`, `getTrackName`, `getTrackPan`, `getTrackPeaks`, `getTrackPluginId`, `getTrackRecordingFileName`, `getTrackStereoSep`, `getTrackVolume`, `isTrackArmed`, `isTrackAutomationEnabled`, `isTrackEnabled`, `isTrackMuteLock`, `isTrackMuted`, `isTrackPluginValid`, `isTrackRevPolarity`, `isTrackSelected`, `isTrackSlotsEnabled`, `isTrackSolo`, `isTrackSwapChannels`, `linkChannelToTrack`, `linkTrackToChannel`, `muteTrack`, `remoteFindEventValue`, `revTrackPolarity`, `selectAll`, `selectTrack`, `setActiveTrack`, `setCurrentTempo`, `setEqBandwidth`, `setEqFrequency`, `setEqGain`, `setPluginMixLevel`, `setPluginMuteState`, `setRouteTo`, `setRouteToLevel`, `setSlotColor`, `setTrackColor`, `setTrackName`, `setTrackNumber`, `setTrackPan`, `setTrackStereoSep`, `setTrackVolume`, `soloTrack`, `swapTrackChannels`, `trackCount`, `trackNumber`

## plugins

`getColor`, `getName`, `getPadInfo`, `getParamCount`, `getParamName`, `getParamValue`, `getParamValueString`, `getPluginName`, `getPresetCount`, `isValid`, `nextPreset`, `prevPreset`, `setParamValue`

## patterns

`burnLoop`, `clonePattern`, `deselectAll`, `ensureValidNoteRecord`, `findFirstNextEmptyPat`, `getActivePatternGroup`, `getBlockSetStatus`, `getChannelLoopStyle`, `getPatternColor`, `getPatternGroupCount`, `getPatternGroupName`, `getPatternLength`, `getPatternName`, `getPatternsInGroup`, `isPatternDefault`, `isPatternSelected`, `jumpToPattern`, `movePattern`, `patternCount`, `patternMax`, `patternNumber`, `selectAll`, `selectPattern`, `setChannelLoop`, `setLoopStarterRootNote`, `setPatternColor`, `setPatternName`

## playlist

`deselectAll`, `getDisplayZone`, `getLiveBlockColor`, `getLiveBlockStatus`, `getLiveLoopMode`, `getLivePosSnap`, `getLiveStatus`, `getLiveTrigSnap`, `getLiveTriggerMode`, `getPerformanceModeState`, `getSongStartTickPos`, `getTrackActivityLevel`, `getTrackActivityLevelVis`, `getTrackColor`, `getTrackName`, `getVisTimeBar`, `getVisTimeStep`, `getVisTimeTick`, `incLiveLoopMode`, `incLivePosSnap`, `incLiveTrigMode`, `incLiveTrigSnap`, `isTrackMuteLock`, `isTrackMuted`, `isTrackSelected`, `isTrackSolo`, `liveBlockNumToTime`, `liveDisplayZone`, `liveTimeToBlockNum`, `lockDisplayZone`, `muteTrack`, `muteTrackLock`, `refreshLiveClips`, `scrollTo`, `selectAll`, `selectTool`, `selectTrack`, `setTrackColor`, `setTrackName`, `soloTrack`, `trackCount`, `triggerLiveClip`

## transport

`continuousMove`, `continuousMovePos`, `fastForward`, `getHWBeatLEDState`, `getLoopMode`, `getSongLength`, `getSongPos`, `getSongPosHint`, `globalTransport`, `isPlaying`, `isRecording`, `markerJumpJog`, `markerSelJog`, `record`, `rewind`, `setLoopMode`, `setPlaybackSpeed`, `setSongPos`, `start`, `stop`

## ui

`closeActivePopupMenu`, `closeAllMenu`, `copy`, `crDisplayRect`, `cut`, `delete`, `down`, `enter`, `escape`, `getFocused`, `getFocusedFormCaption`, `getFocusedFormID`, `getFocusedNodeCaption`, `getFocusedNodeFileType`, `getFocusedPluginName`, `getHintMsg`, `getHintValue`, `getProgTitle`, `getSnapMode`, `getStepEditMode`, `getTimeDispMin`, `getVersion`, `getVisible`, `hideWindow`, `horZoom`, `insert`, `isBrowserAutoHide`, `isClosing`, `isInPopupMenu`, `isLoopRecEnabled`, `isMetronomeEnabled`, `isPrecountEnabled`, `isStartOnInputEnabled`, `jog`, `jog2`, `launchAudioEditor`, `left`, `miDisplayDockRect`, `miDisplayRect`, `moveJog`, `navigateBrowser`, `navigateBrowserMenu`, `navigateBrowserTabs`, `next`, `nextWindow`, `no`, `openEventEditor`, `paste`, `previewBrowserMenuItem`, `previous`, `right`, `scrollWindow`, `selectBrowserMenuItem`, `selectWindow`, `setBrowserAutoHide`, `setFocused`, `setHintMsg`, `setSnapMode`, `setStepEditMode`, `setTimeDispMin`, `showNotification`, `showWindow`, `snapMode`, `snapOnOff`, `strip`, `stripHold`, `stripJog`, `toggleBrowserNode`, `up`, `verZoom`, `yes`

## general

`clearLog`, `dumpScoreLog`, `getChangedFlag`, `getPrecount`, `getProjectAuthor`, `getProjectGenre`, `getProjectTitle`, `getRecPPB`, `getRecPPQ`, `getUndoHistoryCount`, `getUndoHistoryLast`, `getUndoHistoryPos`, `getUndoLevelHint`, `getUseMetronome`, `getVersion`, `processRECEvent`, `restoreUndo`, `restoreUndoLevel`, `safeToEdit`, `saveUndo`, `setDenominator`, `setNumerator`, `setRecPPQ`, `setUndoHistoryCount`, `setUndoHistoryLast`, `setUndoHistoryPos`, `undo`, `undoDown`, `undoUp`, `undoUpDown`

## device

`baseTrackSelect`, `createRefreshThread`, `destroyRefreshThread`, `directFeedback`, `dispatch`, `dispatchGetReceiverPortNumber`, `dispatchReceiverCount`, `findEventID`, `flrMidiCC`, `flrMidiNote`, `forwardMIDICC`, `fullRefresh`, `getDeviceID`, `getIdleElapsed`, `getLinkedChannel`, `getLinkedInfo`, `getLinkedParamName`, `getLinkedValue`, `getLinkedValueString`, `getMasterSync`, `getName`, `getPortNumber`, `hardwareRefreshMixerTrack`, `isAssigned`, `isDoubleClick`, `isMidiOutAssigned`, `linkToLastTweaked`, `midiOutMsg`, `midiOutNewMsg`, `midiOutSysex`, `processMIDICC`, `releaseCC`, `repeatMidiEvent`, `sendMsgGeneric`, `setHasMeters`, `setMasterSync`, `stopRepeatMidiEvent`

## arrangement

`addAutoTimeMarker`, `currentTime`, `currentTimeHint`, `getMarkerName`, `jumpToMarker`, `liveSelection`, `liveSelectionStart`, `selectionClear`, `selectionEnd`, `selectionIsActive`, `selectionSet`, `selectionStart`
