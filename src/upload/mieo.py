"""Mieo: everything the blocks call.

The panel helpers -- emotions, animations, text, the painter -- and the
sensors: buttons, IR, touch, ultrasonic, and the general purpose pins.
The blocks only ever name a pin the way the board is labelled ("D1", "A2",
"T3"), and PINS below is the single place those names become GPIO numbers.
"""

import time

import machine

import display
import linefollower
import motors
import pins
import sound

PANEL_PIN = display.PANEL_PIN
PIN = display.PANEL_PIN
BRIGHTNESS_LIMIT = display.BRIGHTNESS_LIMIT
DEFAULT_BRIGHTNESS = display.DEFAULT_BRIGHTNESS
scalebrightness = display.scalebrightness

FACES = display.FACES
PALETTE = display.PALETTE
PALETTE_ORDER = display.PALETTE_ORDER
ALIASES = display.ALIASES
NAMES = display.NAMES
FRAMES = display.FRAMES
SPECIALS = display.SPECIALS
PATTERNS = display.PATTERNS
PATTERN_NAMES = display.PATTERN_NAMES
FONT = display.FONT
FONT_ORDER = display.FONT_ORDER
glyph_columns = display.glyph_columns
resolveemotion = display.resolveemotion
getcolor = display.getcolor

#: The speaker. Everything is re-exported here rather than left in sound.py
#: because the blocks only ever import this one module -- live mode's
#: handshake runs "import mieo" and nothing else, so anything that is not an
#: attribute of mieo is simply not there when a block runs.
SPEAKER_PIN = sound.SPEAKER_PIN
NOTES = sound.NOTES
DURATIONS = sound.DURATIONS
SOUNDS = sound.SOUNDS
notefreq = sound.notefreq
durationms = sound.durationms
playsound = sound.playsound
playsounduntildone = sound.playsounduntildone
playtone = sound.playtone
playfreq = sound.playfreq
stopsound = sound.stopsound
isplaying = sound.isplaying
setvolume = sound.setvolume
volume = sound.volume

#: The wheels and the servos. Re-exported for the same reason the speaker is:
#: the blocks import mieo and nothing else, so anything not an attribute of
#: mieo does not exist as far as a block is concerned.
FORWARD = motors.FORWARD
BACKWARD = motors.BACKWARD
MOTOR_PINS = motors.MOTOR_PINS
SERVO_PINS = motors.SERVO_PINS
ORIENTATIONS = motors.ORIENTATIONS
motorsbegin = motors.motorsbegin
run = motors.run
forward = motors.forward
backward = motors.backward
drive = motors.drive
motor = motors.motor
runmotor = motors.runmotor
motorforward = motors.motorforward
motorbackward = motors.motorbackward
stopmotor = motors.stopmotor
stoprobot = motors.stoprobot
brake = motors.brake
setmotorpolarity = motors.setmotorpolarity
setrobotorientation = motors.setrobotorientation
motorinfo = motors.motorinfo
setservo = motors.setservo
stopservo = motors.stopservo
releaseservo = motors.releaseservo


#: Following a line. Only the entry points are re-exported, deliberately:
#: pins.py also defines setirthreshold, and pulling that in here would shadow
#: this module's own -- which the "set IR threshold" block calls with "IR-L",
#: a name pins.py does not know.
LINE_SETTINGS = linefollower.LINE_SETTINGS
initializelinefollower = linefollower.initializelinefollower
setlinespeed = linefollower.setlinespeed
setlineinverted = linefollower.setlineinverted
followlinestep = linefollower.followlinestep
atlinecross = linefollower.atlinecross
#: Reading the line sensors. Safe to re-export -- only setirthreshold clashes
#: with this module's own, and that one is deliberately left alone.
readir = linefollower.readir
readirstate = linefollower.readirstate
irstatename = linefollower.irstatename
getirthreshold = linefollower.getirthreshold
followline = linefollower.followline
stoplinefollower = linefollower.stoplinefollower
setlinesensors = linefollower.setlinesensors
startlinefollower = linefollower.startlinefollower
linefollowing = linefollower.linefollowing
linefollowerinfo = linefollower.linefollowerinfo


def gofor(direction, speed, seconds):
    """Drive one way for a while, then stop.

    The stop is in a finally so that interrupting the program -- which is what
    the stop button does -- does not leave the robot driving away.
    """
    motors.run(direction, speed)
    try:
        sleep(int(float(seconds) * 1000))
    finally:
        motors.stoprobot()
    return True


_panel = None

_SPEEDS = {
    "slow": 0.32,
    "normal": 0.18,
    "fast": 0.07,
}


def begin(brightness=None):
    """Wake the panel up. Safe to call more than once.

    `brightness` is the usual 0-100. Left out, the panel starts at
    display.DEFAULT_LEVEL (30 on that scale, so 12% of full power); 100 means
    display.BRIGHTNESS_LIMIT (40% of full power), never more.
    """
    global _panel
    if _panel is None:
        _panel = display.LedMatrix()
    if brightness is None:
        _panel.set_brightness(display.DEFAULT_BRIGHTNESS)
    else:
        setbrightness(brightness)
    return True


def panel():
    """The shared LedMatrix, creating it with defaults if needed."""
    if _panel is None:
        begin()
    return _panel


def sleep(ms):
    """Wait for `ms` milliseconds. Milliseconds, not seconds, so that the
    wait block reads the same here as it does on the other boards."""
    time.sleep(ms / 1000)


def setbrightness(level):
    """0-100. See display.scalebrightness for what 100 really means.

    Takes effect straight away and stays put, even if an animation that swings
    the brightness is playing in the background at the time.
    """
    return display.setpanelbrightness(panel(), display.scalebrightness(level))


def showemotion(name):
    """Draw a face and return. It stays lit until something else is drawn."""
    display.stopsteps()
    display._showface(panel(), name)


def showanimation(name):
    """Start an animation and let the program carry straight on.

    The frames are driven from a timer, so the blocks after this one run while
    it plays -- drive the wheels, read a sensor, make a sound. Whatever draws
    on the panel next takes it over, the way starting a second sound stops the
    first. Use showanimationuntildone to wait for the end of it instead.
    """
    return display.startsteps(display._animatefacesteps(panel(), name))


def showanimationuntildone(name):
    """Play an animation and wait for the end of it."""
    display.stopsteps()
    return display._animateface(panel(), name)


def showpattern(name):
    """Start a light show -- 'rainbow', 'disco', 'party' and friends.

    Plays in the background like showanimation, and blanks the panel when it
    reaches the end or when something stops it.
    """
    return display.startsteps(display._showpatternsteps(panel(), name))


def showpatternuntildone(name):
    """Play a light show and wait for the end of it."""
    display.stopsteps()
    return display._showpattern(panel(), name)


def stopanimation():
    """Stop an animation or pattern that is playing in the background."""
    return display.stopsteps()


def isshowing():
    """True while an animation or pattern is still playing."""
    return display.isbusy()


def _speed(name):
    try:
        return _SPEEDS[name]
    except KeyError:
        raise ValueError(
            "unknown speed '{}'. try one of: {}".format(name, ", ".join(_SPEEDS))
        )


PAINTER_PALETTE = display.PAINTER_PALETTE


def setled(x, y, color="#FFFFFF", brightness=30):
    """Light one pixel and leave the rest alone.

    `x` is 1-7 left to right and `y` is 1-5 top to bottom, matching the numbers
    down the side of the painter. `brightness` is a percentage of this one
    pixel's colour, 30 when left out -- the panel's own ceiling still applies
    on top, so this can only ever make a pixel dimmer, never brighter than the
    board allows.
    """
    display.stopsteps()
    return _setled(x, y, color, brightness)


def _setled(x, y, color="#FFFFFF", brightness=100):
    """Light one pixel without disturbing a running animation.

    The Bluetooth lamp goes through here. It is an overlay on whatever else is
    on the panel, not a drawing block, so a board being connected to should
    not be able to stop an animation part way through.
    """
    rgb = display.tocolor(color)
    level = 100 if brightness is None else brightness
    if level != 100:
        scale = max(0, min(100, level)) / 100.0
        rgb = (int(rgb[0] * scale), int(rgb[1] * scale), int(rgb[2] * scale))
    p = panel()
    col = int(x) - 1
    row = int(y) - 1
    if not (0 <= col < p.width and 0 <= row < p.height):
        raise ValueError(
            "x must be 1-{} and y 1-{}, got x={} y={}".format(p.width, p.height, x, y)
        )
    p.set_pixel(col, row, rgb)
    p.show()
    return True


def clearled(x, y):
    """Turn one pixel off, leaving the rest of the picture up."""
    display.stopsteps()
    return _setled(x, y, (0, 0, 0))


def clearscreen():
    """Blank the whole panel."""
    display.stopsteps()
    p = panel()
    p.clear()
    p.show()


def showmatrix(pattern):
    """Show a picture painted in the block editor's 7x5 grid.

    `pattern` is 35 characters, row by row from the top left, each indexing the
    painter's palette -- "0" is off, "1"-"9" are colours. Every pixel carries
    its own colour, so there is no separate colour argument. The picture stays
    up until something else is drawn.
    """
    display.stopsteps()
    display.drawpainted(panel(), pattern)


def showtext(message, color="#FFC800", speed="normal"):
    """Scroll a message across the panel.

    `color` is whatever the colour picker produced ("#RRGGBB"), a colour name
    like "red", or an (r, g, b) tuple. A single character is shown still
    instead of scrolled, since there is nothing to scroll.
    """
    display.stopsteps()
    color = display.tocolor(color)
    text = str(message)
    if len(text) <= 1:
        display.showchar(panel(), text, color)
    else:
        display.scrolltext(panel(), text, color, delay=_speed(speed))

# ------------------------------------------------------------- the sensors
# Everything below reads the board's inputs. The pin map is fixed by the
# wiring, so these names -- D1, A2, T3, IR-L -- are the only ones the blocks
# ever mention; a GPIO number never reaches a student.
#
# All six analog inputs sit on ADC1 (GPIO 32-39), which keeps working while
# WiFi is on. ADC2 would not. GPIO 34/35/36/39 are input-only and have no
# internal pull-up, which is fine: every one of them is an analog input here.

PINS = {
    "D1": 21, "D2": 22, "D3": 19,     # general purpose digital
    "S1": 16, "S2": 17,               # the servo headers, usable as digital
    "A1": 39, "A2": 32, "A3": 33,     # analog in  (A1 is SENSOR_VN)
    "T1": 12, "T2": 13, "T3": 14, "T4": 15,   # capacitive touch
    "IR-L": 34, "IR-R": 35,           # the two IR sensors
}

#: GPIO 25 is not here on purpose: it belongs to the speaker, and sound.py
#: owns it. It is not a pin a block can address, so naming it here would only
#: invite something to drive it.

#: GPIO 34, 35, 36 and 39 can only ever be read on the ESP32 -- they have no
#: output driver at all. A1 (39) and the IR pins land here, so a pin that has
#: to be driven, like an ultrasonic trigger, cannot be one of these.
INPUT_ONLY_PINS = (34, 35, 36, 39)


#: Both buttons hang off this one analog pin and are told apart by voltage
#: rather than by having a pin each. The readings below are what the board
#: actually produces, which is not what the nominal 10K / 2.2K divider would
#: suggest -- trust the measurements, not the arithmetic.
BUTTON_PIN = 36

#: Measured on the board, not calculated: pressing L reads 0 and pressing R
#: reads about 211, while nothing pressed sits high. L pulls the pin hard to
#: ground rather than through a divider, which is why the two are so close
#: together and why "both pressed" reads the same as L alone -- see button().
#:
#: The L/R boundary sits halfway between the two measurements, giving about
#: 105 counts of margin either side, comfortably past normal ADC noise. The
#: idle boundary is deliberately loose: anything at or above it is nobody
#: pressing anything, and the real idle reading is far higher again.
BUTTON_L_MAX = 105
BUTTON_IDLE_MIN = 1000

#: An IR sensor reads low when it sees something. Each one keeps its own
#: threshold so a block can tune it for the surface it is pointed at.
IR_THRESHOLD = {"IR-L": 3000, "IR-R": 3000}

#: A touch pad reads high when untouched and drops sharply when a finger
#: lands on it.
TOUCH_THRESHOLD = 300

_adc = {}
_touch = {}
_din = {}
_sonar = {}


def _pin(name):
    """GPIO number for a board label like "D1", or a plain number as-is."""
    key = str(name).strip().upper()
    if key in PINS:
        return PINS[key]
    try:
        return int(name)
    except (TypeError, ValueError):
        raise ValueError(
            "unknown pin '{}'. try one of: {}".format(name, ", ".join(sorted(PINS)))
        )


def _analog(gpio):
    """An ADC for this pin, made once and kept."""
    if gpio not in _adc:
        adc = machine.ADC(machine.Pin(gpio))
        # Full 0-3.3V range at 12-bit, rather than the 1.1V default.
        adc.atten(machine.ADC.ATTN_11DB)
        adc.width(machine.ADC.WIDTH_12BIT)
        _adc[gpio] = adc
    return _adc[gpio]


def _touchpad(gpio):
    if gpio not in _touch:
        _touch[gpio] = machine.TouchPad(machine.Pin(gpio))
    return _touch[gpio]


def _digital(gpio):
    if gpio not in _din:
        _din[gpio] = machine.Pin(gpio, machine.Pin.IN)
    return _din[gpio]


# ----------------------------------------------------------------- buttons

def buttonvalue():
    """Raw reading from the button pin, 0-4095.

    Read this if the buttons ever need re-tuning: press each one, note the
    number, and put the halfway point in BUTTON_L_MAX.
    """
    return _analog(BUTTON_PIN).read()


def button(which="L"):
    """True while that button is held. "L" is S1 and "R" is S2.

    Both buttons share one analog pin and are told apart by voltage. L pulls
    the pin all the way to ground, so holding both looks exactly like holding
    L alone -- with this wiring the two cannot be separated, and holding both
    reports L.
    """
    key = str(which).strip().upper()
    value = buttonvalue()
    if value >= BUTTON_IDLE_MIN:
        return False
    return key == ("L" if value <= BUTTON_L_MAX else "R")


# --------------------------------------------------------------------- IR

def irvalue(which="IR-L"):
    """Raw reading from one IR sensor, 0-4095. Lower means more reflection."""
    return _analog(_pin(which)).read()


def setirthreshold(which, value):
    """Set the level below which that IR sensor counts as active."""
    key = str(which).strip().upper()
    if key not in IR_THRESHOLD:
        raise ValueError(
            "unknown IR sensor '{}'. try one of: {}".format(
                which, ", ".join(sorted(IR_THRESHOLD))
            )
        )
    IR_THRESHOLD[key] = int(value)


def iractive(which="IR-L"):
    """True when that IR sensor is seeing something closer than its threshold."""
    key = str(which).strip().upper()
    return irvalue(key) < IR_THRESHOLD.get(key, 3000)


# ------------------------------------------------------------------ touch

def touchvalue(which="T1"):
    """Raw capacitance reading. Drops when touched."""
    return _touchpad(_pin(which)).read()


def touched(which="T1"):
    """True while a finger is on that pad."""
    return touchvalue(which) < TOUCH_THRESHOLD


# ------------------------------------------------------------- ultrasonic

def connectultrasonic(number, echo="D1", trig="D2"):
    """Say which pins an HC-SR04 is wired to. Do this before reading it.

    Echo is read and trig is driven, so trig cannot be one of the input-only
    pins -- A1 is GPIO39, which the chip can never drive. Saying so here beats
    a sensor that simply always reads -1.
    """
    trig_gpio = _pin(trig)
    if trig_gpio in INPUT_ONLY_PINS:
        raise ValueError(
            "{} can only be read, so it cannot be the trig pin. "
            "Use D1, D2, D3, A2 or A3 for trig.".format(trig)
        )
    _sonar[int(number)] = (_pin(echo), trig_gpio)


def ultrasonic(number=1):
    """Distance in cm, or -1 if nothing answered.

    Sound covers 1cm in about 29.1us and the pulse makes the trip twice, so
    the round trip is divided by 58.2.
    """
    try:
        echo_pin, trig_pin = _sonar[int(number)]
    except KeyError:
        raise ValueError(
            "ultrasonic {} has no pins yet -- use the connect block first".format(number)
        )
    trig = machine.Pin(trig_pin, machine.Pin.OUT)
    echo = machine.Pin(echo_pin, machine.Pin.IN)
    trig.value(0)
    time.sleep_us(2)
    trig.value(1)
    time.sleep_us(10)
    trig.value(0)
    try:
        # 30ms is about 5m of air, well past this sensor's range
        duration = machine.time_pulse_us(echo, 1, 30000)
    except OSError:
        return -1
    if duration < 0:
        return -1
    return duration / 58.2


# --------------------------------------------------------- other sensors

def analogsensor(pin="A1"):
    """Raw 0-4095 from an analog input."""
    return _analog(_pin(pin)).read()


def digitalsensor(pin="D1"):
    """True when the pin is high."""
    return _digital(_pin(pin)).value() == 1


# ------------------------------------------------------- general purpose pins

#: The pin blocks in the Mieo category. pins.py already knows how to drive
#: these, so they are re-exported here for the same reason the speaker and the
#: motors are: a block only ever has "mieo" to reach for. Named one at a time
#: rather than pulled in wholesale, because pins.py also defines setirthreshold
#: and that must not shadow this module's own -- see the line follower section.
digitalread = pins.digitalread
analogread = pins.analogread


def digitalstate(pin):
    """True while the pin is high, for the blocks that ask a yes or no.

    digitalread answers 1 or 0, which is the right answer for a pin but the
    wrong shape for an "if" -- the editor reads a sensor back as true only
    when the board prints True. Same reading, said as a yes or a no.
    """
    return pins.digitalread(pin) == 1


def _releaseservo(pin):
    """Take a servo header back before driving it as a plain pin.

    S1 and S2 carry a servo or an ordinary signal, but not both: motors.py
    keeps a 50 Hz PWM on the pin from the moment a servo block runs, and
    leaving it there while pins.py drives the same GPIO gives the pin two
    owners. Nothing to do for any other pin, and never a reason to fail.
    """
    try:
        # resolvepin so that 16 and "s1" release the servo just as "S1" does.
        # Its own error for a name it does not know is left to digitalwrite
        # and setpwm, which say it in the right place.
        name = pins.resolvepin(pin)
        if name in motors.SERVO_PINS:
            motors.releaseservo(name)
            return True
    except Exception:
        pass
    return False


def digitalwrite(pin, value=1):
    """Switch a pin on or off."""
    _releaseservo(pin)
    return pins.digitalwrite(pin, value)


#: The PWM block counts 0-255, the number anyone who has met an Arduino
#: expects, while pins.analogwrite runs 0-1023 and refuses anything above it.
#: The scaling lives here so that both program modes drive the pin through one
#: function and a child is never shown two different ranges for one idea.
PWM_MAX = 255


def setpwm(pin, value):
    """Drive a pin with PWM. `value` is 0-255, 255 being full on."""
    _releaseservo(pin)
    value = int(value)
    if value < 0:
        value = 0
    elif value > PWM_MAX:
        value = PWM_MAX
    return pins.analogwrite(pin, value * 1023 // PWM_MAX)


# --------------------------------------------------------- bluetooth indicator

#: The first pixel of the panel doubles as a Bluetooth lamp: lit while the
#: board has power and nothing has connected to it, dark once something has.
#: Off unless a project asks for it, so a picture still gets all 35 pixels.
BLUETOOTH_INDICATOR_XY = (1, 1)
BLUETOOTH_INDICATOR_COLOR = "#0080FF"

_bt_indicator = False


def bluetoothindicator(state=True):
    """Switch the Bluetooth lamp on the first pixel on or off.

    Takes the block's own words as well as a plain true or false, so that
    "enable" and "disable" work whichever mode the block runs in.
    """
    global _bt_indicator
    if isinstance(state, str):
        state = state.strip().lower() not in ("disable", "off", "false", "0", "")
    _bt_indicator = bool(state)
    if _bt_indicator:
        showbluetooth()
    else:
        _setled(BLUETOOTH_INDICATOR_XY[0], BLUETOOTH_INDICATOR_XY[1], (0, 0, 0))
    return _bt_indicator


def bluetoothindicatoron():
    """True while the lamp is switched on."""
    return _bt_indicator


def showbluetooth():
    """Put the Bluetooth lamp into the right state for this moment.

    Lit while nobody is connected, dark while somebody is. Called when the
    block switches the indicator, and again from mieoble's event handler, so
    the pixel follows the link without anything having to poll it. Does
    nothing while the indicator is off, so it is safe to call unconditionally.
    """
    if not _bt_indicator:
        return False
    try:
        import mieoble
        linked = mieoble.connected()
    except Exception:
        # No radio on this build, or it has not been started yet. Showing the
        # board as still waiting is the honest answer either way.
        linked = False
    x, y = BLUETOOTH_INDICATOR_XY
    if linked:
        _setled(x, y, (0, 0, 0))
    else:
        _setled(x, y, BLUETOOTH_INDICATOR_COLOR)
    return not linked
