"""What stands between the user and a working session, and how to fix it:
the launcher's checklist. Pure functions of what the launcher found out, so
the tests can try every case."""

HEAR_GRACE = 12.0   # seconds FL may run with the MIDI port open before "not heard" counts as a problem
FL_STARTUP = 45.0   # ...and seconds after FL started (it loads plugins before its MIDI scripts)

OK, BAD, WAIT, OFF = "ok", "bad", "wait", "off"


def item(key, state, title, fix=None, detail=None):
    fix = [fix] if isinstance(fix, str) else list(fix or [])
    return {"id": key, "state": state, "title": title, "fix": fix, "detail": detail}


def checks(facts, port="FLSync"):
    """The checklist for `facts` (a dict; missing entries count as unknown):

    loopmidi    loopMIDI.exe runs (None: unknown)
    ports       (inputs, outputs) MIDI port names, or None
    midi_open   the bridge has the MIDI port open
    midi_problem why it couldn't open it (text from the bridge), or None
    fl          FL's process runs (None: unknown)
    fl_started, loopmidi_started  when they started (Unix time) or None
    fl_age      seconds since FL started, or None
    fl_heard    the bridge hears FL now
    fl_label    which FL runs ("FL Studio 2026"), None if unknown or several
    fl_unset    the running FL's label if its MIDI settings lack FLSync (an FL installed after FLSync)
    fl_version  FL's version text
    silent_for  seconds FL has run with the port open and not been heard (None: not applicable)
    settings    winsys.fl_midi_settings() for the port, or None
    mode        None, "host" or "join"
    relay       {"state": OK/BAD/WAIT, "title", "fix"} from the launcher
    session     {"state", "title", "fix"} from the launcher
    """
    found = []
    loopmidi = facts.get("loopmidi")
    if loopmidi is False:
        found.append(item("loopmidi", BAD, "loopMIDI är inte igång",
                          "Starta loopMIDI (Start-menyn > loopMIDI) och låt det vara igång. Saknas det: välj "
                          "Reparera FLSync i Start-menyn. Starta sedan om FL Studio, så att FL hittar porten %s."
                          % port))
    elif loopmidi:
        found.append(item("loopmidi", OK, "loopMIDI är igång"))

    ports = facts.get("ports")
    has_port = None if ports is None else all(any(n.lower() == port.lower() for n in names) for names in ports)
    problem = facts.get("midi_problem")
    if loopmidi is False:
        found.append(item("port", WAIT, "MIDI-porten %s" % port, detail="kräver att loopMIDI är igång"))
    elif has_port is False:
        found.append(item("port", BAD, "Det finns ingen MIDI-port som heter %s" % port,
                          "Välj Reparera FLSync i Start-menyn, eller lägg till porten själv i loopMIDI: skriv %s under "
                          "New port-name och klicka +. Starta sedan om FL Studio (FL ser bara portar som fanns "
                          "när det startade)." % port))
    elif facts.get("midi_open"):
        found.append(item("port", OK, "MIDI-porten %s är öppen" % port))
    elif problem:
        found.append(item("port", BAD, "FLSync kan inte öppna MIDI-porten %s" % port,
                          "Stäng andra program som använder porten (t.ex. bridge.py eller ett annat "
                          "FLSync-fönster) och vänta några sekunder.", detail=problem))
    else:
        found.append(item("port", WAIT, "Öppnar MIDI-porten %s ..." % port))

    fl = facts.get("fl")
    version = facts.get("fl_version")
    name = facts.get("fl_label") or "FL Studio"
    age, silent = facts.get("fl_age"), facts.get("silent_for")
    if facts.get("fl_heard"):
        found.append(item("fl", OK, "%s hörs på MIDI" % name + (" (FL %s)" % version if version else "")))
    elif fl is False:
        found.append(item("fl", WAIT, "FL Studio är inte igång", "Starta FL Studio. FLSync hittar det av sig självt."))
    elif fl is None:
        found.append(item("fl", WAIT, "Väntar på FL Studio"))
    elif facts.get("fl_unset"):
        found.append(item("fl", BAD, "%s är inte inställt för FLSync än" % facts["fl_unset"],
                          ["Stäng %s (spara först). FLSync ställer in det direkt när det är stängt, och sedan "
                           "startar du det igen." % facts["fl_unset"]]))
    elif not facts.get("midi_open"):
        found.append(item("fl", WAIT, "FL Studio är igång, väntar på MIDI-porten"))
    elif age is not None and age < FL_STARTUP:
        found.append(item("fl", WAIT, "FL Studio startar ..."))
    elif silent is None or silent < HEAR_GRACE:
        found.append(item("fl", WAIT, "Väntar på att FL Studio hörs på MIDI ..."))
    else:
        found.append(item("fl", BAD, "FL Studio är igång men hörs inte på MIDI", not_heard_fix(facts, port)))

    relay = facts.get("relay")
    if relay:
        found.append(item("relay", relay.get("state", WAIT), relay.get("title", ""), relay.get("fix"),
                          relay.get("detail")))
    session = facts.get("session")
    if session:
        found.append(item("session", session.get("state", WAIT), session.get("title", ""), session.get("fix"),
                          session.get("detail")))
    return found


def not_heard_fix(facts, port="FLSync"):
    """What to do when FL runs but says nothing on the MIDI port: the most
    likely causes first."""
    steps = []
    fl_started, loopmidi_started = facts.get("fl_started"), facts.get("loopmidi_started")
    if fl_started and loopmidi_started and fl_started < loopmidi_started:
        steps.append("FL startades innan loopMIDI skapade porten %s, och då kommer inget fram. Stäng FL Studio "
                     "och starta det igen." % port)
    steps.extend(settings_problems(facts.get("settings"), port))
    steps.append("I FL: Options > MIDI settings. Under Input: markera %s, välj Controller type FLSync, ge den ett "
                 "Port-nummer och klicka Enable. Under Output: markera %s och ge den samma Port-nummer. "
                 "Reparera FLSync i Start-menyn gör det åt dig, men FL måste startas om efteråt." % (port, port))
    steps.append("Syns inte FLSync under Controller type: starta om FL Studio. Hjälper inte det: välj "
                 "Reparera FLSync i Start-menyn.")
    return steps


def settings_problems(settings, port="FLSync"):
    """What's wrong in FL's saved MIDI settings for the port (FL saves them when
    it closes, so changes made in a running FL may not show yet)."""
    if not settings:
        return []
    problems = []
    found_in, found_out = settings.get("input"), settings.get("output")
    in_port = _port_number(found_in)
    if found_in is None:
        problems.append("FL har inte sett MIDI-porten %s än. Starta om FL Studio när loopMIDI är igång." % port)
    else:
        if found_in.get("Enabled") != "1":
            problems.append("Ingången %s är inte aktiverad i FL (Options > MIDI settings > Input: markera den "
                            "och klicka Enable)." % port)
        if (found_in.get("ScriptFolder") or "").lower() != "flsync":
            problems.append("Ingången %s har inte Controller type FLSync." % port)
        if in_port is None:
            problems.append("Ingången %s har inget Port-nummer." % port)
    out_port = _port_number(found_out)
    if found_in is not None and out_port is None:
        problems.append("Utgången %s har inget Port-nummer. Ge den samma nummer som ingången%s."
                        % (port, " (%d)" % in_port if in_port is not None else ""))
    elif in_port is not None and out_port is not None and in_port != out_port:
        problems.append("Utgången %s har Port-nummer %d men ingången %d. Ge dem samma nummer."
                        % (port, out_port, in_port))
    if out_port is not None:
        for name, values in (settings.get("outputs") or {}).items():
            if name.lower() != port.lower() and _port_number(values) == out_port:
                problems.append("Utgången %s har också Port-nummer %d. Ge %s ett eget nummer."
                                % (name, out_port, port))
    return problems


def _port_number(values):
    try:
        number = int((values or {}).get("Port"))
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None
