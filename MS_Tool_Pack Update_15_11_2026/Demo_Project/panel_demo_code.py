# MS_Tool Program — demo for the panel "Demo" (Panel tab › Import… › Demo.panel.json)
# Start the program, then use the panel (or its Start / Stop button).

counter: int = 0


@on_start
def start():
    write("Panel demo: use the controls of the panel Demo")
    set_timer("tick", 100)


@on_timer("tick")
def tick():
    counter += 1                                  # (no `global` needed)
    sysvar_set("Demo::Counter", counter)
    set_timer("tick", 100)


@on_sysvar("Demo::Speed")
def speed_changed(value):
    sysvar_set("Demo::Warn", 1 if value > 200 else 0)


@on_sysvar("Demo::Gear")
def gear_changed(value):
    sysvar_set("Demo::GearText", ["P", "R", "N", "D"][int(value)])
    write("Gear %s" % ["P", "R", "N", "D"][int(value)])


@on_sysvar("Demo::Horn", update=True)
def horn(value):
    write("Horn " + ("ON" if value else "off"))


@on_sysvar("Demo::Mode")
def mode(value):
    write("Sport mode " + ("on" if value else "off"))


@on_sysvar("Demo::Heat")
def heat(value):
    write("Seat heater " + ("on" if value else "off"))


@on_sysvar("Demo::Note")
def note(value):
    write("Note: " + value)


@on_stop
def stop():
    write("Panel demo stopped after %d ticks" % counter)
