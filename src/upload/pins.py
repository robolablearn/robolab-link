"""The pins: D1-D3, A0-A3, IRL/IRR, T1-T4, the switches and the IR."""

import time

from machine import Pin

# MicroPython counts milliseconds with ticks_ms; desktop Python does not.
# time.time() on the ESP32 only steps a whole second at a time, which is too
# coarse for "wait 3 seconds" or "follow the line for 10 seconds".
try:
    from time import ticks_ms as _ticks, ticks_diff as _tickdiff
except ImportError:
    def _ticks():
        return int(time.time() * 1000)

    def _tickdiff(a, b):
        return a - b


# ------------------------------------------------------- the name -> gpio
PINS = {
    "D1": 21,
    "D2": 22,
    "D3": 19,
    # the servo headers. they carry an ordinary output as happily as they
    # carry a servo, so a block can drive one high or dim an led on it
    "S1": 16,
    "S2": 17,
    "A0": 36,
    "A1": 39,
    "A2": 32,
    "A3": 33,
    "IRL": 34,
    "IRR": 35,
    "T1": 12,
    "T2": 13,
    "T3": 14,
    "T4": 15,
}

#: in board order, for drop down menus
PIN_ORDER = ("D1", "D2", "D3", "S1", "S2", "A0", "A1", "A2", "A3",
             "IRL", "IRR", "T1", "T2", "T3", "T4")

#: SENSOR_VP (36) and SENSOR_VN (39). the chip can only ever read these
#: two -- there is no output driver and no pull resistor on either
INPUT_ONLY = (34, 35, 36, 37, 38, 39)

#: analog inputs that keep working when WiFi is on (the chip's ADC1)
ANALOG_GPIOS = (36, 37, 38, 39, 32, 33, 34, 35)

#: the gpios with a touch sensor built in
TOUCH_GPIOS = (0, 2, 4, 12, 13, 14, 15, 27, 32, 33)

#: what the chip calls its two read-only sensor pins
SENSOR_NAMES = {34: "left IR", 35: "right IR",
                36: "SENSOR_VP", 39: "SENSOR_VN"}

#: pins the chip also watches while it is starting up.  holding one of
#: these hard high or low at power on can stop the board booting
STRAPPING_GPIOS = (0, 2, 12, 15)

HIGH = 1
LOW = 0

IN = "in"
OUT = "out"
INPULLUP = "inpullup"
INPULLDOWN = "inpulldown"

# live objects, kept so a pin keeps its mode and its output level
_pins = {}
_adcs = {}
_touch = {}
_pinpwms = {}
_touch_base = {}


# ------------------------------------------------------------- name tools
def resolvepin(name):
    """'d1', ' D1 ', 21 and '21' all mean the same pin -> 'D1'."""
    key = str(name).strip().upper()
    if key in PINS:
        return key
    try:
        number = int(key)          # a bare gpio number is allowed too
    except ValueError:
        raise ValueError(
            "unknown pin {}. try one of: {}".format(
                repr(name), ", ".join(PIN_ORDER))
        )
    for pin_name in PIN_ORDER:
        if PINS[pin_name] == number:
            return pin_name
    raise ValueError(
        "gpio {} is not brought out on this board. the pins are: {}".format(
            number,
            ", ".join("{} (gpio {})".format(n, PINS[n]) for n in PIN_ORDER))
    )


def gpioof(name):
    """The GPIO number behind a pin name."""
    return PINS[resolvepin(name)]


def pinlist():
    """All the pin names, e.g. to fill a drop down menu."""
    return list(PIN_ORDER)


def can(name, job):
    """Can this pin do 'output', 'analogread' or 'touch'?"""
    number = gpioof(name)
    if job == "output":
        return number not in INPUT_ONLY
    if job == "analogread":
        return number in ANALOG_GPIOS
    if job == "touch":
        return number in TOUCH_GPIOS
    raise ValueError("do not know the job {}".format(repr(job)))


def _needs(name, job):
    """Stop early, with a message that says what to do instead."""
    if can(name, job):
        return
    number = gpioof(name)
    if job == "output":
        raise ValueError(
            "{} (gpio {}) is a sensor pin -- it can only be read, never "
            "driven. use D1, D2, D3, S1, S2, A2 or A3 to switch something "
            "on.".format(name, number)
        )
    if job == "analogread":
        ok = [n for n in PIN_ORDER if PINS[n] in ANALOG_GPIOS]
        raise ValueError(
            "analogread stops working on {} (gpio {}) once WiFi is on. use "
            "one of: {}".format(name, number, ", ".join(ok))
        )
    ok = [n for n in PIN_ORDER if PINS[n] in TOUCH_GPIOS]
    raise ValueError(
        "{} (gpio {}) has no touch sensor. the touch pins are: {}".format(
            name, number, ", ".join(ok))
    )


def _level(value):
    """1, 0, True, False, on, off, high, low -> 1 or 0."""
    if isinstance(value, str):
        word = value.strip().lower()
        if word in ("1", "on", "high", "true", "yes"):
            return 1
        if word in ("0", "off", "low", "false", "no"):
            return 0
        raise ValueError(
            "{} is not on or off. use 1 or 0, on or off, high or low".format(
                repr(value))
        )
    return 1 if value else 0


def _release_pwm(name):
    """Stop any analogwrite still running on this pin."""
    pwm = _pinpwms.pop(name, None)
    if pwm is not None:
        try:
            pwm.deinit()
        except Exception:
            pass


# ---------------------------------------------------------------- digital
def pinmode(name, mode=OUT):
    """Set a pin up by hand: 'out', 'in', 'inpullup' or 'inpulldown'."""
    name = resolvepin(name)
    number = PINS[name]
    mode = str(mode).strip().lower()
    _release_pwm(name)
    if mode in ("out", "output"):
        _needs(name, "output")
        pin = Pin(number, Pin.OUT)
    elif mode in ("in", "input"):
        pin = Pin(number, Pin.IN)
    elif mode in ("inpullup", "pullup", "button"):
        if number in INPUT_ONLY:
            raise ValueError(
                "{} (gpio {}) has no pull up resistor inside the chip. wire a "
                "10k resistor to 3V3 instead.".format(name, number)
            )
        pin = Pin(number, Pin.IN, Pin.PULL_UP)
    elif mode in ("inpulldown", "pulldown"):
        if number in INPUT_ONLY:
            raise ValueError(
                "{} (gpio {}) has no pull down resistor inside the chip. wire "
                "a 10k resistor to GND instead.".format(name, number)
            )
        pin = Pin(number, Pin.IN, Pin.PULL_DOWN)
    else:
        raise ValueError(
            "unknown mode {}. try: out, in, inpullup, inpulldown".format(
                repr(mode))
        )
    _pins[name] = pin
    return pin


def digitalwrite(name, value=1):
    """Switch a pin on or off."""
    name = resolvepin(name)
    _needs(name, "output")
    _release_pwm(name)
    level = _level(value)
    pin = _pins.get(name)
    if pin is None:
        pin = pinmode(name, OUT)
    else:
        pin.init(Pin.OUT)
    pin.value(level)
    return level


def digitalread(name):
    """Read a pin: 1 if there is a voltage on it, 0 if not."""
    name = resolvepin(name)
    pin = _pins.get(name)
    if pin is None:
        pin = pinmode(name, IN)
    return pin.value()


def digitaltoggle(name):
    """Flip a pin from on to off, or off to on."""
    name = resolvepin(name)
    return digitalwrite(name, 0 if digitalread(name) else 1)


# ----------------------------------------------------------------- analog
def analogread(name):
    """Read a sensor as a number from 0 (0 V) to 4095 (3.3 V)."""
    from machine import ADC

    name = resolvepin(name)
    _needs(name, "analogread")
    adc = _adcs.get(name)
    if adc is None:
        adc = ADC(Pin(PINS[name]))
        try:                       # the widest range, so 0-3.3 V is measured
            adc.atten(ADC.ATTN_11DB)
        except AttributeError:
            pass
        try:
            adc.width(ADC.WIDTH_12BIT)
        except AttributeError:
            pass
        _adcs[name] = adc
    try:
        return adc.read()
    except AttributeError:
        return adc.read_u16() >> 4


def analogpercent(name):
    """Read a sensor as 0 to 100 instead of 0 to 4095."""
    return int(analogread(name) * 100 / 4095)


def analogwrite(name, value, freq=1000):
    """Dim an LED or drive a motor: 0 is off, 1023 is full on."""
    from machine import PWM

    name = resolvepin(name)
    _needs(name, "output")
    value = int(value)
    if not (0 <= value <= 1023):
        raise ValueError("analogwrite wants 0 to 1023, not {}".format(value))
    pwm = _pinpwms.get(name)
    if pwm is None:
        _pins.pop(name, None)
        pwm = PWM(Pin(PINS[name]))
        _pinpwms[name] = pwm
    pwm.freq(freq)
    pwm.duty(value)
    return value


# ------------------------------------------------------------------ touch
def touchread(name):
    """Raw touch reading. It drops towards 0 when a finger lands on it."""
    from machine import TouchPad

    name = resolvepin(name)
    _needs(name, "touch")
    pad = _touch.get(name)
    if pad is None:
        pad = TouchPad(Pin(PINS[name]))
        _touch[name] = pad
    return pad.read()


def touchcalibrate(name, samples=8):
    """Remember what this pad reads with nothing touching it."""
    name = resolvepin(name)
    total = 0
    for _ in range(samples):
        total += touchread(name)
    _touch_base[name] = total / samples
    return _touch_base[name]


def istouched(name, level=None):
    """True while a finger is on the pad."""
    name = resolvepin(name)
    value = touchread(name)
    base = _touch_base.get(name)
    if base is None:
        base = touchcalibrate(name)
    if level is None:
        level = 0.6
    return value < base * level


# ------------------------------------------------------------------- tidy
def pinsoff():
    """Switch every output pin off and stop any analogwrite."""
    for name in PIN_ORDER:
        if not can(name, "output"):
            continue
        _release_pwm(name)
        if name in _pins:
            digitalwrite(name, 0)


def pininfo(name=None):
    """Print what each pin is and what it can do."""
    names = PIN_ORDER if name is None else (resolvepin(name),)
    for pin_name in names:
        number = PINS[pin_name]
        jobs = []
        if can(pin_name, "output"):
            jobs.append("digital in/out")
            jobs.append("analogwrite")
        else:
            jobs.append("digital in only")
        if can(pin_name, "analogread"):
            jobs.append("analogread")
        if can(pin_name, "touch"):
            jobs.append("touch")
        note = ""
        if number in INPUT_ONLY:
            note = "   ({}, read only)".format(SENSOR_NAMES.get(number, "sensor pin"))
        elif number in STRAPPING_GPIOS:
            note = "   (the chip watches this one while booting)"
        print("   {:<3} gpio {:<3} {}{}".format(
            pin_name, number, ", ".join(jobs), note))


# --------------------------------------------------------------- switches
# S1 and S2 both hang off A0 (SENSOR_VP), each through its own resistor to
# ground -- S1 through 2.2k, S2 through 10k.  Only one wire is used for two
# switches, so instead of "on or off" the pin gives a different VOLTAGE for
# each one, and the driver works out which switch that voltage belongs to.
#
#      3V3 --[ pull up ]--+-- A0 (gpio 36)
#                         |
#                         +--o/o S1 --[ 2.2k ]-- GND
#                         |
#                         +--o/o S2 --[ 10k  ]-- GND
#
# Pressing S1 pulls A0 much lower than S2 does, because 2.2k is the smaller
# resistor.  Nothing pressed leaves A0 sitting up at 3V3.

SWITCH_PIN = "A0"

#: what A0 reads in each state, 0-4095.  MEASURED on the board rather than
#: worked out from the resistors: pressing S1 reads about 0 and S2 about 211,
#: nothing like the nominal divider figures, because S1 pulls the pin hard to
#: ground rather than through its resistor.  run learnswitches() if your own
#: board disagrees.
SWITCH_LEVELS = {
    "none": 4095,   # nothing pressed, A0 sits at 3V3
    "s1": 0,        # S1 down: pulled hard to ground
    "s2": 211,      # S2 down
    "both": 0,      # both down looks the same as S1 alone with this wiring
}

#: how far a reading may sit from a known level and still count as that
#: state.  S1 and S2 are only ~211 apart, so this has to be well under half
#: that or the two would overlap.
SWITCH_TOLERANCE = 90

SWITCH_ORDER = ("none", "s1", "s2", "both")


def switchvalue(samples=3):
    """The raw number A0 is reading right now, 0 to 4095."""
    readings = sorted(analogread(SWITCH_PIN) for _ in range(max(1, samples)))
    return readings[len(readings) // 2]        # the middle one, ignores noise


def readswitch(samples=3):
    """Which switch is being held down: 'none', 's1', 's2' or 'both'."""
    value = switchvalue(samples)
    best = None
    best_gap = None
    for state in SWITCH_ORDER:
        level = SWITCH_LEVELS.get(state)
        if level is None:
            continue
        gap = abs(value - level)
        if best_gap is None or gap < best_gap:
            best, best_gap = state, gap
    if best_gap is not None and best_gap > SWITCH_TOLERANCE:
        return "none"          # nothing close enough to be sure about
    return best


def ispressed(which):
    """True while that switch is held down."""
    key = str(which).strip().lower().replace("switch", "").replace(" ", "")
    if key not in ("s1", "s2", "1", "2"):
        raise ValueError(
            "{} is not a switch. this board has S1 and S2".format(repr(which))
        )
    if key in ("1", "2"):
        key = "s" + key
    state = readswitch()
    return state == key or state == "both"


def setswitchlevel(state, value):
    """Teach the driver what one state reads on your board."""
    state = str(state).strip().lower()
    if state not in SWITCH_ORDER:
        raise ValueError(
            "{} is not a switch state. try: {}".format(
                repr(state), ", ".join(SWITCH_ORDER))
        )
    SWITCH_LEVELS[state] = int(value)
    return SWITCH_LEVELS[state]


def learnswitches(settle=None):
    """Measure what your own board reads, one state at a time."""
    import time

    asks = (
        ("none", "let go of both switches"),
        ("s1", "hold S1 down"),
        ("s2", "hold S2 down"),
        ("both", "hold S1 and S2 down together"),
    )
    found = {}
    for state, what in asks:
        print("   {}...".format(what))
        try:
            input("      press Enter when ready ")
        except (EOFError, OSError):
            time.sleep(settle if settle is not None else 3)
        found[state] = switchvalue(samples=9)
        print("      {:<5} reads {}".format(state, found[state]))
    SWITCH_LEVELS.update(found)

    order = sorted(found, key=lambda s: found[s])
    gaps = [found[order[i + 1]] - found[order[i]] for i in range(len(order) - 1)]
    print()
    print("   paste these into SWITCH_LEVELS at the top of pins.py:")
    print("   SWITCH_LEVELS = {")
    for state in SWITCH_ORDER:
        print('       "{}": {},'.format(state, found[state]))
    print("   }")
    if gaps and min(gaps) < 150:
        near = order[gaps.index(min(gaps))]
        print()
        print("   careful: {} and {} only read {} apart, which is close to the"
              .format(near, order[order.index(near) + 1], min(gaps)))
        print("   noise in the chip's ADC. those two may sometimes swap.")
    return dict(found)


# ------------------------------------------------------------ IR sensors
# Two reflectance sensors looking down at the floor, wired to the analog
# pins IRL (left, gpio 34) and IRR (right, gpio 35).  Each one shines
# infrared down and measures how much bounces back, so it reads one number
# rather than just on or off: a pale floor bounces plenty back, a black
# line swallows it.
#
# Turning that number into "am I over the line?" needs a threshold -- the
# value half way between what the sensor reads over pale floor and what it
# reads over the line.  Every sensor, every floor and every ride height
# gives different numbers, so run calibrateir() once and it works them out.

IR_PINS = ("IRL", "IRR")

#: the number each sensor has to cross to count as being over the line.
#: These are the numbers the working Arduino line follower uses on this
#: robot, where IRR is the sensor on the left and IRL the one on the right.
#: calibrateir() or the "set line follower" block replace them with yours.
IR_THRESHOLDS = {"IRL": 2200, "IRR": 1700}

#: does the reading go UP over the dark line, or down?  it depends on how
#: the sensor module is built, so calibrateir() works it out by looking.
IR_DARK_IS_HIGH = {"IRL": True, "IRR": True}

DARK = 1      # over the line
LIGHT = 0     # over the floor


def _ir(name):
    """Check this really is one of the two IR sensors.

    "IR-L" is accepted as well as "IRL": the block editor labels the sensors
    with a hyphen and this module without one, and a student should never meet
    an error about which spelling a library happens to prefer.
    """
    if isinstance(name, str):
        name = name.replace("-", "").replace("_", "")
    name = resolvepin(name)
    if name not in IR_PINS:
        raise ValueError(
            "{} is not an IR sensor. this board has IRL (left) and IRR "
            "(right)".format(repr(name))
        )
    return name


def readir(name):
    """The raw number an IR sensor is reading, 0 to 4095."""
    return analogread(_ir(name))


def readirstate(name):
    """1 while the sensor is over the dark line, 0 while it is over floor."""
    name = _ir(name)
    reads_high = readir(name) >= IR_THRESHOLDS[name]
    return DARK if reads_high == IR_DARK_IS_HIGH[name] else LIGHT


def irstatename(name):
    """The same answer as a word: 'dark' or 'light'."""
    return "dark" if readirstate(name) == DARK else "light"


def setirthreshold(name, value):
    """Set the crossing point for one sensor by hand, 0 to 4095."""
    name = _ir(name)
    value = int(value)
    if not (0 <= value <= 4095):
        raise ValueError(
            "an IR threshold is 0 to 4095, not {}".format(value)
        )
    IR_THRESHOLDS[name] = value
    return value


def getirthreshold(name):
    """What the crossing point for one sensor is set to."""
    return IR_THRESHOLDS[_ir(name)]


def setirpolarity(name, dark_is_high=True):
    """Say which way round this sensor reads."""
    name = _ir(name)
    IR_DARK_IS_HIGH[name] = bool(dark_is_high)
    return IR_DARK_IS_HIGH[name]


def waitforpress(message="press S1 when ready", seconds=25, switch="S1"):
    """Wait for a switch on the robot to be pressed. True if it was."""
    print("   {}...".format(message))
    deadline = _ticks() + int(seconds * 1000)
    while _tickdiff(deadline, _ticks()) > 0:
        try:
            if ispressed(switch):
                while ispressed(switch):        # let go before carrying on
                    time.sleep(0.02)
                return True
        except Exception:                       # noqa: BLE001  (no switch wired)
            time.sleep(seconds)
            return False
        time.sleep(0.02)
    print("      (nothing pressed in {}s, carrying on anyway)".format(seconds))
    return False


def calibrateir(name=None, samples=16, button=True, settle=3):
    """Learn what the floor and the line read, and set the thresholds."""
    names = IR_PINS if name is None else (_ir(name),)

    def average():
        totals = dict((n, 0) for n in names)
        for _ in range(samples):
            for n in names:
                totals[n] += readir(n)
        return dict((n, totals[n] // samples) for n in names)

    def hold(what):
        if button:
            waitforpress("hold the sensors over the {}, then press S1".format(what))
        else:
            print("   hold the sensors over the {}".format(what))
            try:
                input("      press Enter when ready ")
            except (EOFError, OSError):
                time.sleep(settle)

    print("calibrating the IR sensors")
    hold("PALE FLOOR")
    light = average()
    for n in names:
        print("      {} over floor: {}".format(n, light[n]))

    hold("BLACK LINE")
    dark = average()
    for n in names:
        print("      {} over line:  {}".format(n, dark[n]))

    print()
    ok = True
    for n in names:
        gap = abs(dark[n] - light[n])
        IR_THRESHOLDS[n] = (dark[n] + light[n]) // 2
        IR_DARK_IS_HIGH[n] = dark[n] > light[n]
        print("   {}: crosses at {}, the number goes {} over the line".format(
            n, IR_THRESHOLDS[n], "up" if IR_DARK_IS_HIGH[n] else "down"))
        if gap < 200:
            ok = False
            print("      !! floor and line only read {} apart -- too close to".format(gap))
            print("         tell apart. lower the sensor towards the floor, or")
            print("         use a blacker line on paler paper.")

    print()
    print("   paste these two lines into mieo.py to keep them:")
    print("   IR_THRESHOLDS = {" + ", ".join(
        '"{}": {}'.format(n, IR_THRESHOLDS[n]) for n in IR_PINS) + "}")
    print("   IR_DARK_IS_HIGH = {" + ", ".join(
        '"{}": {}'.format(n, IR_DARK_IS_HIGH[n]) for n in IR_PINS) + "}")
    if not ok:
        print("   (the numbers are set, but the gap above is too small to trust)")
    return dict((n, IR_THRESHOLDS[n]) for n in names)


def irinfo():
    """Print what both IR sensors are seeing right now."""
    for n in IR_PINS:
        value = readir(n)
        print("   {:<3} gpio {}  reads {:<5} threshold {:<5} -> {}".format(
            n, PINS[n], value, IR_THRESHOLDS[n], irstatename(n)))
