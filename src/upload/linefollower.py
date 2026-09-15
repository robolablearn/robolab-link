"""Following a black line with the two IR sensors.

The same line follower as the Arduino library and as mieo_line_follower.py,
which is the one that works on this robot: four cases, pivot turns, and a
stop when both sensors see black.

    both over the floor          -> forward
    the left sensor on the line  -> right wheel back, left wheel forward
    the right sensor on the line -> right wheel forward, left wheel back
    both on the line             -> stop (a junction, or the finish bar)

The two sensors are plugged in the other way round from their names on this
robot: the one called IRR sits on the LEFT. SENSOR_LEFT and SENSOR_RIGHT say
which is which, and setlinesensors() swaps them if the plugs ever change.

Finding the thresholds is the fiddly part. A reading is 0 to 4095, higher
over black. Watch mieo.readir("IRR") and mieo.readir("IRL") over the floor
and over the line, and put a number half way between into the "set line
follower" block, or into initializelinefollower().
"""

import time

import machine
import micropython

from pins import *
from pins import _ticks, _tickdiff
from motors import *

#: Which sensor is on which side of the robot. Plugged in the other way
#: round from their names on this robot, so the sensor called IRR is the one
#: sitting on the left.
SENSOR_LEFT = "IRR"
SENSOR_RIGHT = "IRL"

#: How it drives, 0-100. speed is both wheels going straight. In a pivot the
#: outer wheel drives forward at outer and the inner wheel BACKWARD at inner,
#: so the robot spins on the spot rather than swinging round a stopped wheel.
#:
#: The Arduino version pivoted at 40 forward / 30 back, with its motor PWM at
#: 1 kHz. Ours runs at 20 kHz (motors.py says why), where a small geared motor
#: has noticeably less torque at low duty: 30 % in reverse under a pivot's
#: load can stall while the forward wheel still turns, which looks like only
#: one motor running. So the inner wheel reverses at the outer wheel's speed,
#: and neither goes below PIVOT_MIN.
LINE_SETTINGS = {
    "speed": 60,
    "outer": 70,
    "inner": 70,
    "inverted": False,   # True swaps the two steering cases over
}

#: The slowest a pivot drives either wheel. Below this the reversed wheel
#: does not reliably move at 20 kHz.
PIVOT_MIN = 40

#: How long the Arduino loop waits between one look and the next.
STEP_WAIT = 0.01

#: Timer 2. Timer 0 belongs to sound.py, Timer 1 to the display, and
#: Timer(-1) decodes to Timer(3) on this port, which the Bluetooth REPL owns.
_LINE_TIMER_ID = 2

#: How often the timer steps, in ms: the Arduino loop's 10 ms.
_LINE_TICK_MS = 10

#: How long the board keeps following without hearing from the editor.
DEFAULT_KEEPALIVE_MS = 1000

_lineready = False
_linetimer = None
_linerunning = False
_linedeadline = 0
_linepending = False
_linelast = "idle"


def _linethreshold(value):
    """0-100 is read as a percentage, anything bigger as a raw 0-4095 count."""
    value = int(value)
    if 0 <= value <= 100:
        return value * 4095 // 100
    if value <= 4095:
        return value
    raise ValueError(
        "an IR threshold is 0-100 as a percentage, or up to 4095 as a raw "
        "reading. {} is neither".format(value)
    )


def setlinesensors(left=None, right=None):
    """Say which IR sensor is on which side of the robot.

    setlinesensors("IRR", "IRL") is how this robot is wired; swap the two if
    the plugs are the other way round on another one.
    """
    global SENSOR_LEFT, SENSOR_RIGHT
    left = SENSOR_LEFT if left is None else resolvepin(left)
    right = SENSOR_RIGHT if right is None else resolvepin(right)
    if left not in IR_PINS or right not in IR_PINS or left == right:
        raise ValueError(
            "the line follower needs the two IR sensors, one each side. "
            "they are IRL and IRR, not {} and {}".format(repr(left), repr(right))
        )
    SENSOR_LEFT, SENSOR_RIGHT = left, right
    return (SENSOR_LEFT, SENSOR_RIGHT)


def setlinespeed(speed, turn=None, inner=None):
    """How fast it goes: speed for both wheels going straight, 0-100.

    A pivot drives the outer wheel `turn` faster than that (10 by default,
    so speed 60 pivots at 70) and the inner wheel BACKWARD at the same speed,
    neither ever below PIVOT_MIN. Pass `inner` to choose the reversed wheel's
    speed yourself; inner=0 stops it instead of reversing it, for a robot
    that should swing round a wheel rather than spin.
    """
    speed = max(0, min(100, int(speed)))
    if turn is None:
        turn = max(0, LINE_SETTINGS["outer"] - LINE_SETTINGS["speed"])
    turn = max(0, int(turn))
    LINE_SETTINGS["speed"] = speed
    LINE_SETTINGS["outer"] = max(PIVOT_MIN, min(100, speed + turn))
    if inner is None:
        LINE_SETTINGS["inner"] = LINE_SETTINGS["outer"]
    else:
        LINE_SETTINGS["inner"] = max(0, min(100, int(inner)))
    return (LINE_SETTINGS["speed"], LINE_SETTINGS["outer"], LINE_SETTINGS["inner"])


def _pivotwords():
    """'40 forward / 40 back', or '40 forward / inside wheel stopped'."""
    inner = LINE_SETTINGS["inner"]
    return "{} forward / {}".format(
        LINE_SETTINGS["outer"], "{} back".format(inner) if inner else "inside wheel stopped")


def _pivotwheel(which, inner):
    """The inside wheel of a pivot: backward at `inner`, or stopped at 0."""
    if inner > 0:
        runmotor(which, BACKWARD, inner)
    else:
        stopmotor(which)


def setlineinverted(inverted=True):
    """Swap the two steering cases over, for a robot that steers the wrong way."""
    LINE_SETTINGS["inverted"] = bool(inverted)
    return LINE_SETTINGS["inverted"]


def initializelinefollower(left_threshold=None, right_threshold=None, speed=None, turn=None):
    """Set the robot up for line following.

    left_threshold and right_threshold are for the sensor on the left and the
    sensor on the right of the robot, whichever names those are: 0-100 as a
    percentage, or up to 4095 as one of the raw readings mieo.readir gives.
    speed is how fast it goes straight, 0-100. turn is how much faster the
    outer wheel goes in a pivot.
    """
    global _lineready
    if left_threshold is not None:
        setirthreshold(SENSOR_LEFT, _linethreshold(left_threshold))
    if right_threshold is not None:
        setirthreshold(SENSOR_RIGHT, _linethreshold(right_threshold))
    if speed is not None:
        setlinespeed(speed, turn)
    elif turn is not None:
        setlinespeed(LINE_SETTINGS["speed"], turn)
    motorsbegin()
    _lineready = True
    print("line follower ready")
    print("   left sensor is {}, crosses at {:>4}; right sensor is {}, crosses at {:>4}  (0-4095)".format(
        SENSOR_LEFT, getirthreshold(SENSOR_LEFT), SENSOR_RIGHT, getirthreshold(SENSOR_RIGHT)))
    print("   straight at {}, pivoting at {}".format(LINE_SETTINGS["speed"], _pivotwords()))
    return (getirthreshold(SENSOR_LEFT), getirthreshold(SENSOR_RIGHT))


def _linesees():
    """(left_dark, right_dark) for the sensor on the left and on the right."""
    left_dark = bool(readirstate(SENSOR_LEFT))
    right_dark = bool(readirstate(SENSOR_RIGHT))
    if LINE_SETTINGS["inverted"]:
        left_dark, right_dark = right_dark, left_dark
    return left_dark, right_dark


def _linedecide(left_dark, right_dark):
    """The four cases, in the order the Arduino version checks them."""
    if not left_dark and not right_dark:
        return "forward"
    if left_dark and not right_dark:
        return "left"
    if right_dark and not left_dark:
        return "right"
    return "stop"


def _stepnow():
    """One look, one steer, no waiting. What followlinestep and the timer share."""
    global _lineready, _linelast
    if not _lineready:
        # Setting the flag matters: motorsbegin() stops the wheels on its way
        # in, so without it every single step would begin by braking.
        motorsbegin()
        _lineready = True

    left_dark, right_dark = _linesees()
    doing = _linedecide(left_dark, right_dark)
    speed = LINE_SETTINGS["speed"]
    outer = LINE_SETTINGS["outer"]
    inner = LINE_SETTINGS["inner"]

    if doing == "forward":
        run(FORWARD, speed)
    elif doing == "left":
        _pivotwheel("R", inner)
        runmotor("L", FORWARD, outer)
    elif doing == "right":
        runmotor("R", FORWARD, outer)
        _pivotwheel("L", inner)
    else:
        stoprobot()
    _linelast = doing
    return doing


def followlinestep():
    """Look once and steer once, exactly as the Arduino loop does.

    Returns what it decided: "forward", "left", "right" or "stop". Both
    sensors on black stops the wheels, so a robot that reaches a junction
    stands still there rather than driving on. Waits the same 10 ms the
    Arduino loop waits, so a block loop paces the same way. This is what
    Upload mode calls once per pass of its loop.
    """
    doing = _stepnow()
    time.sleep(STEP_WAIT)
    return doing


# ------------------------------------------------- following in the background

def _linetick(_timer_arg):
    """The timer's callback. Hands the real work to the scheduler.

    Nothing here reads a sensor or touches a motor: that happens in the
    scheduler, between whatever else the board is doing. The pending flag
    keeps the scheduler queue from filling up if the board is busy.
    """
    global _linepending
    if not _linerunning or _linepending:
        return
    _linepending = True
    try:
        micropython.schedule(_lineadvance, 0)
    except Exception:
        # queue full. the next tick will try again
        _linepending = False


def _lineadvance(_arg):
    """One step, from the scheduler. Deliberately incapable of raising."""
    global _linepending, _linerunning
    _linepending = False
    if not _linerunning:
        return
    try:
        if _tickdiff(_linedeadline, _ticks()) <= 0:
            # The editor has gone quiet: it closed, crashed, or lost the link.
            # Stopping here is what keeps the robot off the floor's edge.
            stoplinefollower()
            return
        if _stepnow() == "stop":
            # Both sensors on black. Stand still and hand over: the blocks
            # inside "do line following" may now drive the robot through
            # the junction without being fought for it every 10 ms. The next
            # kick, once the robot is off the cross, starts following again.
            _releaselinetimer()
    except Exception:
        stoplinefollower()


def _releaselinetimer():
    """Let the timer go. Cheap to make again, and idle it costs nothing."""
    global _linetimer, _linerunning
    _linerunning = False
    timer = _linetimer
    _linetimer = None
    if timer is not None:
        try:
            timer.deinit()
        except Exception:
            pass


def startlinefollower(keepalive=DEFAULT_KEEPALIVE_MS):
    """Follow the line from the board's own timer, until told to stop.

    Steps every 10 ms exactly as followlinestep() does, but without the
    editor in the loop, which is what makes Arena mode follow as well as
    Upload mode. Calling it again while running only refreshes the
    keepalive: the editor calls it every quarter second while the block
    runs, and the board stops the wheels by itself if `keepalive` ms pass
    without a call. Returns True when a run was started, False when one
    was already running.
    """
    global _linetimer, _linerunning, _linedeadline, _linepending
    _linedeadline = _ticks() + int(keepalive)
    if _linerunning:
        return False
    if not _lineready:
        motorsbegin()
    _linepending = False
    _releaselinetimer()
    timer = machine.Timer(_LINE_TIMER_ID)
    timer.init(period=_LINE_TICK_MS, mode=machine.Timer.PERIODIC, callback=_linetick)
    _linetimer = timer
    _linerunning = True
    return True


def linefollowing():
    """True while the board is following the line by itself."""
    return _linerunning


def atlinecross():
    """True while both sensors see black.

    One sensor dark is a bend to steer around; both together is something
    else -- a junction, a finish bar, or the robot sitting square on a wide
    patch of black -- and that is the moment a program wants to be told
    about, because following the line no longer means anything there.
    """
    left_dark, right_dark = _linesees()
    return left_dark and right_dark


def followline(seconds=None, show=False):
    """Follow the line until you stop it (or for `seconds`)."""
    if not _lineready:
        print("tip: run mieo.initializelinefollower(1700, 2200) first, with "
              "numbers from your own floor and line")
        motorsbegin()

    deadline = None if seconds is None else _ticks() + int(seconds * 1000)
    last = None
    try:
        while deadline is None or _tickdiff(deadline, _ticks()) > 0:
            doing = followlinestep()
            if show and doing != last:
                print("   {:<8} left {} {:>4}  right {} {:>4}".format(
                    doing, SENSOR_LEFT, readir(SENSOR_LEFT),
                    SENSOR_RIGHT, readir(SENSOR_RIGHT)))
                last = doing
    except KeyboardInterrupt:
        pass
    finally:
        stoprobot()
    return True


def stoplinefollower():
    """Stop following, whether by timer or by blocks, and stop the wheels."""
    _releaselinetimer()
    stoprobot()


def linefollowerinfo():
    """Print how the line follower is set up, and what it sees right now."""
    print("   straight at {}, pivoting at {}{}".format(
        LINE_SETTINGS["speed"], _pivotwords(),
        ", steering swapped" if LINE_SETTINGS["inverted"] else ""))
    for side, name in (("left", SENSOR_LEFT), ("right", SENSOR_RIGHT)):
        print("   {:<5} {:<3} reads {:>4}  crosses at {:>4}  -> {}".format(
            side, name, readir(name), getirthreshold(name), irstatename(name)))
    print("   it would go: {}".format(_linewoulddo()))


def _linewoulddo():
    """What followlinestep() would decide, without touching the motors."""
    left_dark, right_dark = _linesees()
    return _linedecide(left_dark, right_dark)
