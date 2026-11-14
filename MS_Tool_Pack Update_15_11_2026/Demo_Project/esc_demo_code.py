# MS_Tool Program — demo "ESC": simulator of an ESC ECU (ESC / ABS / TCS)
# Panel tab › Import… › ESC.panel.json, paste this code in the Code tab, start the program.
#
# A simple car model (bicycle model + road friction µ) every 20 ms:
#   ESC  the yaw rate leaves the target (understeer / oversteer) → brakes one wheel
#   ABS  brake demand above the grip → holds the slip at ~10 % (no ABS: the wheels lock)
#   TCS  throttle above the grip → cuts the torque (ESC OFF: the front wheels spin)
# CAN TX (CAN1, example IDs, change them to your DBC):
#   0x1A0  wheel speeds FL FR RL RR, uint16 little endian, 0.01 km/h
#   0x1A1  b0 ABS | TCS<<1 | ESC<<2 | OFF<<3 | FAULT<<4, b1-2 yaw int16 0.01 deg/s,
#          b3-4 lateral acc. int16 0.01 m/s², b5 ESC wheel, b6 alive counter, b7 checksum
import math

G = 9.81
L = 2.8                 # wheelbase m
K = 0.0025              # understeer gradient (s²/m²)
RATIO = 15.0            # steering ratio
HALF_TRACK = 0.8        # m
MU = [1.0, 0.6, 0.3, 0.1]

v = 0.0                 # m/s
yaw = 0.0               # rad/s
t = 0.0
alive = 0
last = {"abs": 0, "tcs": 0, "esc": 0}


@on_start
def start():
    write("ESC demo: Speed 80 km/h, then steer / brake / throttle on wet, snow or ice")
    set_timer("sim", 20)


@on_sysvar("Esc::Set80")
def set80(value):
    if value:
        v = 80 / 3.6


@on_sysvar("Esc::EscOff")
def esc_off(value):
    write("ESC OFF" if value else "ESC ON")


@on_sysvar("Esc::Fault")
def fault(value):
    write("FAULT injected: ABS / TCS / ESC disabled" if value else "Fault cleared")


def edge(name, on):
    if on and not last[name]:
        write(name.upper() + " active")
    last[name] = on


def s16(x):
    raw = int(round(x)) & 0xFFFF
    return [raw & 0xFF, raw >> 8]


@on_timer("sim")
def sim():
    set_timer("sim", 20)                      # first: an error below must not stop the simulation
    dt = 0.02
    t = t + dt
    mu = MU[int(sysvar_get("Esc::Road"))]
    thr = sysvar_get("Esc::Throttle")
    brk = sysvar_get("Esc::Brake")
    steer = sysvar_get("Esc::Steer")
    off = int(sysvar_get("Esc::EscOff"))
    flt = int(sysvar_get("Esc::Fault"))
    abs_ok = not flt
    esc_ok = not flt and not off              # ESC OFF also turns TCS off
    grip = mu * G                             # m/s² the tyres can give

    # ── longitudinal: TCS and ABS
    tcs, abs_on, lock, spin = 0, 0, False, 0.0
    a_dem = thr / 100 * 4.0
    if a_dem > 0.8 * grip:
        if esc_ok:
            tcs, a, spin = 1, 0.8 * grip, 0.08
        else:
            a = 0.5 * grip
            spin = min(1.5, (a_dem - 0.8 * grip) / grip * 2)
    else:
        a = a_dem
    b_dem = brk / 100 * 10.0
    if b_dem > 0.9 * grip and v > 1:
        if abs_ok:
            abs_on, b = 1, 0.9 * grip
        else:
            lock, b = True, 0.7 * grip        # locked wheels slide: less grip
    else:
        b = b_dem
    v = v + a * dt
    dec = (b + 0.1 + 0.0004 * v * v) * dt
    v = max(0.0, v - dec)

    # ── lateral: yaw rate target vs what the road allows
    delta = math.radians(steer / RATIO)
    yaw_t = v * delta / (L * (1 + K * v * v))
    yaw_max = 0.85 * grip / max(v, 1.0)
    sign = 1 if yaw_t >= 0 else -1
    oversteer = False
    if lock:
        goal = 0.0                            # locked front wheels do not steer
    elif abs(yaw_t) <= yaw_max:
        goal = yaw_t
    elif mu <= 0.3:
        oversteer = True                      # the rear slides out: more yaw than asked
        goal = sign * (yaw_max + (abs(yaw_t) - yaw_max) * 1.3)
    else:
        goal = sign * yaw_max                 # understeer: the car pushes wide
    esc, wheel = 0, 0
    if esc_ok and v > 15 / 3.6 and abs(goal - yaw_t) > math.radians(3):
        esc = 1
        left = yaw_t > 0
        if oversteer:
            wheel = 2 if left else 1          # outer front
        else:
            wheel = 3 if left else 4          # inner rear
        goal = sign * min(abs(yaw_t), yaw_max)
        v = max(0.0, v - 1.5 * dt)            # the braked wheel slows the car
    yaw = yaw + (goal - yaw) * min(1.0, dt * 6)
    yaw = max(-1.7, min(1.7, yaw))            # ±97 deg/s: the car spins
    lat = max(-25.0, min(25.0, v * yaw))

    # ── wheel speeds
    w = [v - yaw * HALF_TRACK, v + yaw * HALF_TRACK, v - yaw * HALF_TRACK, v + yaw * HALF_TRACK]
    w[0] *= 1 + spin
    w[1] *= 1 + spin                          # front wheel drive
    if abs_on:
        w = [x * (0.88 + 0.05 * math.sin(t * 40 + i)) for i, x in enumerate(w)]
    if lock:
        w = [0.0, 0.0, 0.0, 0.0]
    if wheel:
        w[wheel - 1] *= 0.85
    w = [max(0.0, x) for x in w]
    slip = max(abs(x - v) for x in w) / max(v, 1.0) * 100 if v > 0.5 else 0.0

    edge("abs", abs_on)
    edge("tcs", tcs)
    edge("esc", esc)
    for name, x in zip(("FL", "FR", "RL", "RR"), w):
        sysvar_set("Esc::Wheel" + name, x * 3.6)
    sysvar_set("Esc::Speed", v * 3.6)
    sysvar_set("Esc::YawTarget", max(-100.0, min(100.0, math.degrees(yaw_t))))
    sysvar_set("Esc::YawRate", math.degrees(yaw))
    sysvar_set("Esc::LatAcc", lat)
    sysvar_set("Esc::Slip", min(100.0, slip))
    sysvar_set("Esc::AbsActive", abs_on)
    sysvar_set("Esc::TcsActive", tcs)
    sysvar_set("Esc::EscActive", esc)
    sysvar_set("Esc::EscWheel", wheel)
    sysvar_set("Esc::Lamp", 1 if tcs or esc else 0)
    alive = (alive + 1) & 0x0F
    sysvar_set("Esc::Alive", alive)

    if sysvar_get("Esc::Tx"):
        data = []
        for x in w:
            data += s16(min(655.35, x * 3.6) * 100)
        send(0x1A0, data)
        status = [abs_on | tcs << 1 | esc << 2 | off << 3 | flt << 4]
        status += s16(math.degrees(yaw) * 100) + s16(lat * 100) + [wheel, alive]
        status.append((sum(status) & 0xFF) ^ 0xFF)
        send(0x1A1, status)


@on_stop
def stop():
    write("ESC demo stopped")
