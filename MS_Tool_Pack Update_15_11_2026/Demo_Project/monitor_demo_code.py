# MS_Tool Program — demo "Monitor": watch values, min / max / average, alarms
# Panel tab › Import… › Monitor.panel.json, paste this code in the Code tab, start the program.
#
# Simulation on: the code makes a battery (voltage, current, temperature, SOC); Load drives it.
# Statistics: min / max / average of every sample (Hold freezes them, Reset clears them).
# Alarms: voltage below V<, temperature above T>; each new alarm counts, Ack clears the count.
import math
import random

t = 0.0
n = 0
v_min = v_max = v_sum = 0.0
i_min = i_max = i_sum = 0.0
t_max = -100.0
alarm_v = alarm_t = 0


@on_start
def start():
    reset_stats()
    write("Monitor demo: move Load, change the alarm limits")
    set_timer("sample", int(sysvar_get("Mon::Period")))


def reset_stats():
    n = 0
    v_min, v_max, v_sum = 1e9, -1e9, 0.0
    i_min, i_max, i_sum = 1e9, -1e9, 0.0
    t_max = -100.0
    sysvar_set("Mon::Samples", 0)


@on_sysvar("Mon::Reset")
def on_reset(value):
    if value:
        reset_stats()
        write("Statistics reset")


@on_sysvar("Mon::Ack")
def on_ack(value):
    if value:
        sysvar_set("Mon::AlarmCount", 0)
        write("Alarms acknowledged")


@on_sysvar("Mon::Period")
def on_period(value):
    write("Sample period %d ms" % value)               # the next sample uses it


def simulate(dt):
    t = t + dt
    load = sysvar_get("Mon::Load")
    soc = sysvar_get("Mon::Soc")
    cur = load * 2.5 + 30 * math.sin(t * 0.7) + random.uniform(-5, 5)
    volt = 405 - cur * 0.08 - (100 - soc) * 0.4 + random.uniform(-0.5, 0.5)
    temp = sysvar_get("Mon::Temp")
    temp = temp + (25 + load * 0.35 - temp) * min(1.0, dt * 0.3) + random.uniform(-0.05, 0.05)
    soc = soc - cur * dt * 0.004                       # fast, to see it move
    if soc < 5:
        soc = 100.0                                    # "charged" again
    sysvar_set("Mon::Current", cur)
    sysvar_set("Mon::Voltage", volt)
    sysvar_set("Mon::Temp", temp)
    sysvar_set("Mon::Soc", max(0.0, min(100.0, soc)))


@on_timer("sample")
def sample():
    period = int(sysvar_get("Mon::Period"))
    set_timer("sample", period)                        # first: an error below must not stop it
    if sysvar_get("Mon::Sim"):
        simulate(period / 1000)
    volt = sysvar_get("Mon::Voltage")
    cur = sysvar_get("Mon::Current")
    temp = sysvar_get("Mon::Temp")
    hold = sysvar_get("Mon::Hold")

    if not hold:
        n = n + 1
        v_min, v_max, v_sum = min(v_min, volt), max(v_max, volt), v_sum + volt
        i_min, i_max, i_sum = min(i_min, cur), max(i_max, cur), i_sum + cur
        t_max = max(t_max, temp)
        sysvar_set("Mon::VMin", v_min)
        sysvar_set("Mon::VMax", v_max)
        sysvar_set("Mon::VAvg", v_sum / n)
        sysvar_set("Mon::IMin", i_min)
        sysvar_set("Mon::IMax", i_max)
        sysvar_set("Mon::IAvg", i_sum / n)
        sysvar_set("Mon::TMax", t_max)
        sysvar_set("Mon::Samples", n)

    av = 1 if volt < sysvar_get("Mon::VLow") else 0
    at = 1 if temp > sysvar_get("Mon::THigh") else 0
    if av and not alarm_v:
        alarm("Low voltage %.1f V" % volt)
    if at and not alarm_t:
        alarm("Over temperature %.1f °C" % temp)
    alarm_v, alarm_t = av, at
    sysvar_set("Mon::AlarmV", av)
    sysvar_set("Mon::AlarmT", at)

    sysvar_set("Mon::RawV", int(round(volt * 10)) & 0xFFFF)
    sysvar_set("Mon::Status", av | at << 1 | (1 if hold else 0) << 2 | (1 if sysvar_get("Mon::Sim") else 0) << 3)


def alarm(text):
    sysvar_set("Mon::AlarmCount", sysvar_get("Mon::AlarmCount") + 1)
    write("ALARM " + text)


@on_stop
def stop():
    write("Monitor demo stopped after %d samples" % n)


# ── Real signals instead of the simulation: turn Simulation off and copy the signals
#    (change the names to your DBC), e.g.
#
# @on_signal("CAN1::BMS_Status.PackVoltage")
# def pack_v(value):
#     sysvar_set("Mon::Voltage", value)
