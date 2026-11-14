# MS_Tool Program — demo "Drive": gear shift + accelerator / brake pedals
# Panel tab › Import… › Drive.panel.json, paste this code in the Code tab, start the program.
#
# Rules of the gear lever (like an EV):
#   POWER → READY only with the brake pressed
#   leaving P, or D / R from standstill: READY + brake pressed
#   P only below 3 km/h, D <-> R only below 5 km/h, N always
# A refused shift puts the lever back, blinks "Shift refused" and says why.
import math

GEARS = ["P", "R", "N", "D"]
MASS = 1800.0            # kg
v = 0.0                  # m/s, < 0 when reversing
powered = False
odo = 0.0                # km


@on_start
def start():
    sysvar_set("Drive::Gear", 0)
    sysvar_set("Drive::GearReq", 0)
    sysvar_set("Drive::GearText", "P")
    sysvar_set("Drive::Msg", "Press the brake, then POWER")
    set_timer("phys", 50)


def say(text, denied=False):
    sysvar_set("Drive::Msg", text)
    if denied:
        sysvar_set("Drive::Denied", 1)
        set_timer("denied", 1500)
        write("Shift refused: " + text)


@on_timer("denied")
def denied_off():
    sysvar_set("Drive::Denied", 0)


@on_sysvar("Drive::Ready")
def power(value):
    braking = sysvar_get("Drive::Brake") >= 10
    if value and not braking:
        sysvar_set("Drive::Ready", 0)                 # refused: back to off
        say("Press the brake to start", True)
        return
    if bool(value) == powered:
        return                                         # (the refusal above wrote 0 again)
    powered = bool(value)
    if value:
        say("READY — brake, then D or R")
        write("READY")
    else:
        if abs(v) * 3.6 < 3:
            set_gear(0)
        say("Power off")
        write("Power off")


def set_gear(g):
    sysvar_set("Drive::Gear", g)
    sysvar_set("Drive::GearReq", g)
    sysvar_set("Drive::GearText", GEARS[g])


@on_sysvar("Drive::GearReq")
def shift(value):
    req = int(value)
    cur = int(sysvar_get("Drive::Gear"))
    if req == cur:
        return
    speed = abs(v) * 3.6
    braking = sysvar_get("Drive::Brake") >= 10
    ready = sysvar_get("Drive::Ready") == 1
    why = ""
    if req == 0 and speed >= 3:
        why = "P only below 3 km/h"
    elif {req, cur} == {1, 3} and speed >= 5:
        why = "D <-> R only below 5 km/h"
    elif cur == 0 and not ready:
        why = "Not READY: press the brake and POWER"
    elif cur == 0 and not braking:
        why = "Press the brake to leave P"
    elif req in (1, 3) and speed < 5 and not braking:
        why = "Press the brake to engage " + GEARS[req]
    if why:
        sysvar_set("Drive::GearReq", cur)             # the lever goes back
        say(why, True)
        return
    set_gear(req)
    say("Gear " + GEARS[req])
    write("Gear %s -> %s at %.1f km/h" % (GEARS[cur], GEARS[req], speed))


@on_timer("phys")
def phys():
    set_timer("phys", 50)                              # first: an error below must not stop it
    dt = 0.05
    ready = sysvar_get("Drive::Ready") == 1
    accel = sysvar_get("Drive::Accel") if ready else 0.0
    brake = sysvar_get("Drive::Brake")
    gear = int(sysvar_get("Drive::Gear"))
    direction = {1: -1, 3: 1}.get(gear, 0)            # R backwards, D forwards, P / N no drive

    # drive force: less at high speed, R limited to about 30 km/h
    top = 8.5 if gear == 1 else 55.0
    a_drive = direction * accel / 100 * 4.5 * max(0.0, 1 - abs(v) / top)
    if ready and direction and accel == 0 and brake < 5:  # creep like an automatic
        a_drive = direction * 0.6 * max(0.0, 1 - abs(v) / 2.0)

    v = v + a_drive * dt
    resist = (brake / 100 * 9.0 + 0.15 + 0.0004 * v * v) * dt
    v = 0.0 if abs(v) <= resist else v - math.copysign(resist, v)
    if gear == 0:
        v = 0.0                                        # parking pawl
    odo = odo + abs(v) * dt / 1000

    power = MASS * a_drive * v / 1000                  # kW
    if brake > 5 and abs(v) > 0.5:
        power = -min(60.0, MASS * brake / 100 * 9.0 * abs(v) / 1000 * 0.3)   # regeneration

    sysvar_set("Drive::Speed", abs(v) * 3.6)
    sysvar_set("Drive::Rpm", min(10000.0, abs(v) * 160))
    sysvar_set("Drive::Power", max(-60.0, min(150.0, power)))
    sysvar_set("Drive::Odo", odo)
    sysvar_set("Drive::BrakeLamp", 1 if brake > 5 else 0)


@on_stop
def stop():
    write("Drive demo stopped, trip %.3f km" % odo)
