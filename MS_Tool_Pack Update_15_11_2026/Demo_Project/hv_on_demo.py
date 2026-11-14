"""HV_ON demo: CVC, BMS, EDS on PT (powertrain CAN) and CVC, BCM on BD (body CAN).

Setup: network PT = PT.dbc, network BD = BD.dbc (Hardware Configuration > Channel Mapping), HOME > Start.
Panel: Panel tab > Import... > HV_ON.panel.json.   Then Run: the test "CVC HV_ON success" runs at once;
"Run HV_ON test" on the panel runs it again.

Units (tabs of the Code tab):
  Main        E2E of the messages, starts the simulation units that are on
  BCM (sim)   ignition, brake pedal, crash (from the panel or the test) -> BCM_Status on BD
  CVC (sim)   ignition On -> HV_On request -> BMS precharge -> EDS Enable -> Ready, READY lamp on BD
  BMS (sim)   contactors: Neg + Pre, precharge until DC link >= 95 %, Pos, Pre open -> HV_On
  EDS (sim)   DC link voltage (precharge RC), Standby -> Ready when enabled and charged
  Test        @sequence "CVC HV_ON success": every step checked on the bus, PASS / FAIL
A real ECU on the bench: turn its "(sim)" unit off (right-click its tab > Turn off): the test then checks it.
"""

STARTERS = []          # every simulation unit adds its start function (a unit that is off adds nothing)

# E2E on every frame the program sends: Alive Counter 0..14, then CRC-8/SAE-J1850 over the other bytes
e2e_message("CVC_PT_Cmd", crc="CVC_PT_CRC", alive="CVC_PT_Alive", algo="CRC-8/SAE-J1850", alive_range=(0, 14))
e2e_message("BMS_Status", crc="BMS_CRC", alive="BMS_Alive", algo="CRC-8/SAE-J1850", alive_range=(0, 14))
e2e_message("EDS_Status", crc="EDS_CRC", alive="EDS_Alive", algo="CRC-8/SAE-J1850", alive_range=(0, 14))
e2e_message("BCM_Status", alive="BCM_Alive", alive_range=(0, 14))
e2e_message("CVC_BD_Status", alive="CVC_BD_Alive", alive_range=(0, 14))


@on_start
def start():
    write("HV_ON demo: %d simulation unit(s) on", len(STARTERS))
    for fn in STARTERS:
        fn()


# ==== unit: BCM (sim) ====
# BCM on BD: the ignition, brake pedal and crash signals come from the panel (or the test) through sysvars.

def bcm_start():
    set_signal("BCM_Status", "BCM_IgnitionSts", sysvar_get("HV::Ignition"))
    set_signal("BCM_Status", "BCM_BrakePedal", sysvar_get("HV::Brake"))
    set_signal("BCM_Status", "BCM_CrashSts", sysvar_get("HV::Crash"))
    send_cyclic("BCM_Status")


STARTERS.append(bcm_start)


@on_sysvar("HV::Ignition")
def bcm_ignition(value):
    set_signal("BCM_Status", "BCM_IgnitionSts", value)
    send("BCM_Status")                                  # event + cyclic, as a real BCM on a change


@on_sysvar("HV::Brake")
def bcm_brake(value):
    set_signal("BCM_Status", "BCM_BrakePedal", value)
    send("BCM_Status")


@on_sysvar("HV::Crash")
def bcm_crash(value):
    set_signal("BCM_Status", "BCM_CrashSts", value)
    send("BCM_Status")


# ==== unit: CVC (sim) ====
# CVC: PT + BD.  Ignition On -> HV_On -> wait BMS HV_On (3 s) -> EDS Enable -> wait EDS Ready (1 s) -> Ready.
# Ignition Off -> HV_Off + EDS ActiveDischarge -> Off.  Crash -> EmergencyOff.  A timeout -> Fault + HV off.
CVC_BMS_TIMEOUT = 3000          # ms from HV_On to BMS HV_On
CVC_EDS_TIMEOUT = 1000          # ms from EDS Enable to EDS Ready
cvc_state = "Off"               # value-table text of CVC_VehicleState
cvc_t0 = 0                      # ms: start of the power-up
cvc_tstep = 0                   # ms: start of the current step
cvc_now = 0                     # ms since start (20 ms steps)


def cvc_start():
    send_cyclic("CVC_PT_Cmd")
    send_cyclic("CVC_VehState")
    send_cyclic("CVC_BD_Status")
    set_timer("cvc", 20)


STARTERS.append(cvc_start)


def cvc_go(state, hv, eds, fault="NoFault"):
    cvc_state = state
    cvc_tstep = cvc_now
    set_signal("CVC_PT_Cmd", "CVC_HVReq", hv)
    set_signal("CVC_PT_Cmd", "CVC_EDSModeReq", eds)
    set_signal("CVC_VehState", "CVC_VehicleState", state)
    set_signal("CVC_VehState", "CVC_FaultCode", fault)
    set_signal("CVC_BD_Status", "CVC_VehicleSts", state)
    set_signal("CVC_BD_Status", "CVC_ReadyLamp", 1 if state == "Ready" else 0)
    set_signal("CVC_BD_Status", "CVC_HVWarnLamp", 1 if state == "Fault" else 0)
    send("CVC_PT_Cmd")                                  # the request goes out now, not in up to 20 ms
    write("CVC: %s%s", state, "" if fault == "NoFault" else " (" + fault + ")")


@on_timer("cvc")
def cvc_tick():
    set_timer("cvc", 20)                                # first: an error below must not stop the CVC
    cvc_now = cvc_now + 20
    ign = get_signal("BCM_Status", "BCM_IgnitionSts")
    crash = get_signal("BCM_Status", "BCM_CrashSts")
    bms = get_signal("BMS_Status", "BMS_HVState")
    eds = get_signal("EDS_Status", "EDS_State")
    set_signal("CVC_BD_Status", "CVC_SOCDisp", get_signal("BMS_Status", "BMS_SOC"))
    waited = cvc_now - cvc_tstep

    if crash == 1 and cvc_state not in ("Off", "Fault", "HV_Off"):
        cvc_go("Fault", "EmergencyOff", "ActiveDischarge", "Crash")
    elif cvc_state == "Off":
        if ign >= 2 and crash == 0:                     # On or Start
            cvc_t0 = cvc_now
            cvc_go("HV_Precharge", "HV_On", "Standby")
    elif cvc_state == "HV_Precharge":
        if ign < 2:
            cvc_go("HV_Off", "HV_Off", "ActiveDischarge")
        elif bms == 4:                                  # Fault
            cvc_go("Fault", "HV_Off", "ActiveDischarge", "BMS_Fault")
        elif get_signal("BMS_Status", "BMS_InsulationOK") == 0:
            cvc_go("Fault", "HV_Off", "ActiveDischarge", "Insulation")
        elif bms == 2:                                  # HV_On: contactors closed
            cvc_go("Standby", "HV_On", "Enable")        # (Standby = HV on, waiting for the EDS)
        elif waited > CVC_BMS_TIMEOUT:
            cvc_go("Fault", "HV_Off", "ActiveDischarge", "Precharge_Timeout")
    elif cvc_state == "Standby":
        if ign < 2:
            cvc_go("HV_Off", "HV_Off", "ActiveDischarge")
        elif bms != 2:
            cvc_go("Fault", "HV_Off", "ActiveDischarge", "BMS_Fault")
        elif eds == 2:                                  # Ready
            set_signal("CVC_VehState", "CVC_HVOnTime", cvc_now - cvc_t0)
            cvc_go("Ready", "HV_On", "Enable")
            write("CVC: READY, HV on in %d ms", cvc_now - cvc_t0)
        elif waited > CVC_EDS_TIMEOUT:
            cvc_go("Fault", "HV_Off", "ActiveDischarge", "EDS_NotReady")
    elif cvc_state == "Ready":
        if ign < 2:
            cvc_go("HV_Off", "HV_Off", "ActiveDischarge")
        elif bms != 2:
            cvc_go("Fault", "HV_Off", "ActiveDischarge", "BMS_Fault")
    elif cvc_state in ("HV_Off", "Fault"):
        if bms == 0 and get_signal("EDS_Status", "EDS_DCLinkVoltage") < 60 and ign < 2:
            cvc_go("Off", "NoRequest", "Standby")       # discharged, key off: back to Off


# ==== unit: BMS (sim) ====
# BMS: HV_On request -> Neg + Pre closed (Precharge) -> DC link >= 95 % of the pack -> Pos closed -> Pre open
# -> HV_On.  Precharge longer than 2 s -> Fault.  HV_Off -> Opening -> Off.  EmergencyOff: all open at once.
# HV::Fault 1 = insulation fail (never ready), 2 = precharge stuck (seen by the EDS sim: no DC link).
BMS_PRE_TIMEOUT = 2000
bms_state = "Off"
bms_t = 0
bms_soc = 80.0


def bms_start():
    bms_set("Off", 0, 0, 0)
    send_cyclic("BMS_Status")
    send_cyclic("BMS_Limits")
    set_timer("bms", 20)


STARTERS.append(bms_start)


def bms_set(state, neg, pre, pos):
    bms_state = state
    bms_t = 0
    set_signal("BMS_Status", "BMS_HVState", state)
    set_signal("BMS_Status", "BMS_ContNeg", neg)
    set_signal("BMS_Status", "BMS_ContPre", pre)
    set_signal("BMS_Status", "BMS_ContPos", pos)
    send("BMS_Status")
    write("BMS: %s (Neg %d, Pre %d, Pos %d)", state, neg, pre, pos)


@on_timer("bms")
def bms_tick():
    set_timer("bms", 20)
    bms_t = bms_t + 20
    req = get_signal("CVC_PT_Cmd", "CVC_HVReq")
    fault = sysvar_get("HV::Fault")
    pack = 280 + bms_soc * 1.2                          # 376 V at 80 %
    insulation = 0 if fault == 1 else 1
    set_signal("BMS_Status", "BMS_PackVoltage", pack)
    set_signal("BMS_Status", "BMS_SOC", bms_soc)
    set_signal("BMS_Status", "BMS_InsulationOK", insulation)
    set_signal("BMS_Status", "BMS_ReadyForHV", 1 if insulation and bms_state in ("Off", "Precharge") else 0)
    set_signal("BMS_Status", "BMS_PackCurrent", 2.5 if bms_state == "HV_On" else 0)

    if req == 3 and bms_state != "Off":                 # EmergencyOff
        bms_set("Off", 0, 0, 0)
    elif bms_state == "Off":
        if req == 1 and insulation:
            bms_set("Precharge", 1, 1, 0)
    elif bms_state == "Precharge":
        link = get_signal("EDS_Status", "EDS_DCLinkVoltage")
        if req != 1:
            bms_set("Opening", 1, 0, 0)
        elif link >= 0.95 * pack:
            set_signal("BMS_Status", "BMS_ContPos", 1)  # Pos closed, then Pre open on the next step
            bms_set("HV_On", 1, 1, 1)
        elif bms_t > BMS_PRE_TIMEOUT:
            bms_set("Fault", 0, 0, 0)
    elif bms_state == "HV_On":
        if get_signal("BMS_Status", "BMS_ContPre") == 1 and bms_t >= 20:
            bms_set("HV_On", 1, 0, 1)                   # precharge contactor opens 20 ms after Pos
        if req in (0, 2):
            bms_set("Opening", 1, 0, 0)
    elif bms_state == "Opening":
        if bms_t >= 100:
            bms_set("Off", 0, 0, 0)
    elif bms_state == "Fault":
        if req in (0, 2):                               # the CVC let go: ready for the next try
            bms_set("Off", 0, 0, 0)


# ==== unit: EDS (sim) ====
# EDS: DC link = capacitor charged through the precharge resistor (tau 250 ms) or straight by Pos; discharged
# fast by ActiveDischarge (tau 100 ms), slowly otherwise.  Standby -> Ready when Enable and DC link >= 90 %.
# HV::Fault 2 = precharge stuck (the DC link does not rise), 3 = EDS not ready (stays Standby).
eds_link = 0.0
eds_state = "Init"
eds_t = 0


def eds_start():
    set_signal("EDS_Status", "EDS_State", "Init")
    send_cyclic("EDS_Status")
    send_cyclic("EDS_Temp")
    set_timer("eds", 20)


STARTERS.append(eds_start)


def eds_set(state):
    if state != eds_state:
        eds_state = state
        set_signal("EDS_Status", "EDS_State", state)
        send("EDS_Status")
        write("EDS: %s", state)


@on_timer("eds")
def eds_tick():
    set_timer("eds", 20)
    eds_t = eds_t + 20
    pack = get_signal("BMS_Status", "BMS_PackVoltage")
    neg = get_signal("BMS_Status", "BMS_ContNeg")
    pre = get_signal("BMS_Status", "BMS_ContPre")
    pos = get_signal("BMS_Status", "BMS_ContPos")
    mode = get_signal("CVC_PT_Cmd", "CVC_EDSModeReq")
    fault = sysvar_get("HV::Fault")

    if neg and pos:
        eds_link = pack
    elif neg and pre and fault != 2:
        eds_link = eds_link + (pack - eds_link) * 0.08  # 20 ms / 250 ms
    elif mode == 2:
        eds_link = eds_link * 0.82                      # active discharge
    else:
        eds_link = eds_link * 0.995
    set_signal("EDS_Status", "EDS_DCLinkVoltage", eds_link)

    if eds_state == "Init":
        if eds_t >= 200:
            eds_set("Standby")
    elif mode == 2:
        eds_set("Discharge" if eds_link > 60 else "Standby")
    elif mode == 1 and fault != 3 and eds_link >= 0.9 * pack and pack > 0:
        eds_set("Ready")
    elif mode == 0 or eds_link < 0.9 * pack:
        eds_set("Standby")


# ==== unit: Test ====
# @sequence "CVC HV_ON success": the HV power-up of the CVC, every step checked on the bus (PT and BD).
# It runs once after Run, and again each time "Run HV_ON test" is pressed on the panel; it stops at the first
# step that fails (PASS / FAIL on the panel and in the Log, with the CVC state and fault code).
test_requested = False
test_fails = 0
test_step = 0


@on_sysvar("HV::RunTest")
def test_button(value):
    if value:
        test_requested = True


def step(text, ok):
    """One checked step: Log, the panel (step text, progress), the fail count."""
    test_step = test_step + 1
    if not ok:
        test_fails = test_fails + 1
    sysvar_set("HV::TestStep", "%d. %s: %s" % (test_step, text, "PASS" if ok else "FAIL"))
    sysvar_set("HV::TestProgress", test_step)
    write("[%d] %s: %s", test_step, text, "PASS" if ok else "FAIL")
    return ok


def wait_until(msg, sig, low, timeout):
    """Wait until msg.sig >= low (a value that rises: no exact match to expect).  True / False."""
    t = 0
    while t <= timeout:
        if get_signal(msg, sig) >= low:
            return True
        wait(20)
        t = t + 20
    return False


def hv_on_test():
    """The power-up, step by step; it stops at the first step that fails."""
    test_fails = 0
    test_step = 0
    sysvar_set("HV::TestResult", 0)
    sysvar_set("HV::TestProgress", 0)
    write("==== CVC HV_ON success: start ====")

    # precondition: key off, HV off
    sysvar_set("HV::Ignition", 0)
    sysvar_set("HV::Crash", 0)
    if not step("Precondition: CVC Off, BMS Off",
                expect_signal("CVC_VehState", "CVC_VehicleState", "Off", timeout=5000)
                and expect_signal("BMS_Status", "BMS_HVState", "Off", timeout=1000)):
        return finish()
    # 1. key On (BD)
    sysvar_set("HV::Ignition", 2)
    if not step("BCM ignition On on BD", expect_signal("BCM_Status", "BCM_IgnitionSts", "On", timeout=300)):
        return finish()
    # 2. CVC asks for HV (PT)
    if not step("CVC HV request = HV_On", expect_signal("CVC_PT_Cmd", "CVC_HVReq", "HV_On", timeout=500)):
        return finish()
    if not step("CVC state = HV_Precharge",
                expect_signal("CVC_VehState", "CVC_VehicleState", "HV_Precharge", timeout=500)):
        return finish()
    # 3. BMS precharge: Neg + Pre closed
    ok = expect_signal("BMS_Status", "BMS_HVState", "Precharge", timeout=500)
    ok = ok and get_signal("BMS_Status", "BMS_ContNeg") == 1 and get_signal("BMS_Status", "BMS_ContPre") == 1
    if not step("BMS Precharge: Neg + Pre closed", ok):
        return finish()
    # 4. DC link charges to 95 % of the pack
    pack = get_signal("BMS_Status", "BMS_PackVoltage")
    if not step("EDS DC link >= 95 %% of %.0f V" % pack,
                wait_until("EDS_Status", "EDS_DCLinkVoltage", 0.95 * pack, 2000)):
        return finish()
    # 5. BMS HV_On: Pos closed, Pre open
    ok = expect_signal("BMS_Status", "BMS_HVState", "HV_On", timeout=1000)
    ok = ok and expect_signal("BMS_Status", "BMS_ContPre", "Open", timeout=200)
    ok = ok and get_signal("BMS_Status", "BMS_ContPos") == 1 and get_signal("BMS_Status", "BMS_ContNeg") == 1
    if not step("BMS HV_On: Neg + Pos closed, Pre open", ok):
        return finish()
    # 6. CVC enables the EDS, the EDS is ready
    if not step("CVC EDS mode = Enable", expect_signal("CVC_PT_Cmd", "CVC_EDSModeReq", "Enable", timeout=500)):
        return finish()
    if not step("EDS state = Ready", expect_signal("EDS_Status", "EDS_State", "Ready", timeout=1000)):
        return finish()
    # 7. Ready on PT and on BD (cluster)
    if not step("CVC state = Ready", expect_signal("CVC_VehState", "CVC_VehicleState", "Ready", timeout=500)):
        return finish()
    if not step("READY lamp on (BD)", expect_signal("CVC_BD_Status", "CVC_ReadyLamp", "On", timeout=500)):
        return finish()
    if not step("No HV warning, fault code NoFault",
                get_signal("CVC_BD_Status", "CVC_HVWarnLamp") == 0
                and get_signal("CVC_VehState", "CVC_FaultCode") == 0):
        return finish()
    t = get_signal("CVC_VehState", "CVC_HVOnTime")
    step("HV on time %d ms <= 3000 ms" % t, 0 < t <= 3000)
    return finish()


def finish():
    passed = test_fails == 0
    sysvar_set("HV::TestResult", 1 if passed else 2)
    write("==== CVC HV_ON success: %s (%d of 13 step(s) run, %d failed) ====", "PASS" if passed else "FAIL",
          test_step, test_fails)
    if not passed:
        write("CVC state %s, fault code %s", get_signal("CVC_VehState", "CVC_VehicleState"),
              get_signal("CVC_VehState", "CVC_FaultCode"))
    return passed


@sequence("CVC HV_ON success")
def test_cvc_hv_on():
    wait(500)                                           # every node sends for a while first
    hv_on_test()
    while True:                                         # "Run HV_ON test" on the panel: again
        if test_requested:
            test_requested = False
            hv_on_test()
        wait(100)
