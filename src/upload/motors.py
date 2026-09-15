"""The two motors, through a DRV8833, and the servos."""

from machine import Pin, PWM

# --------------------------------------------------------------- the pins
LEFT_IN1 = 18
LEFT_IN2 = 27

RIGHT_IN1 = 23
RIGHT_IN2 = 26

MOTOR_PINS = {
    "L": (LEFT_IN1, LEFT_IN2),
    "R": (RIGHT_IN1, RIGHT_IN2),
}

MOTOR_ORDER = ("L", "R")

#: which motors are mounted facing the other way.  the left one is, on this
#: robot, so both wheels turn the same way when you say forward
MOTOR_REVERSED = {"L": True, "R": False}

# ---------------------------------------------------------------- the PWM
#: 20 kHz, not the 1 kHz the Arduino version used, and the reason matters.
#:
#: The chip gives every PWM channel a share of a small pool of timers, and it
#: hands the SAME timer to any two channels asking for the same frequency.
#: The speaker is a PWM channel too, and it sweeps 40-5000 Hz -- so at 1 kHz
#: the motors and the speaker would end up on one timer, and every note would
#: re-programme the motor frequency underneath the wheels.  20 kHz is outside
#: the speaker's range, so the two can never collide.  It is also above
#: hearing, which stops the motors whining, and the DRV8833 is specified well
#: past it.
PWM_FREQ = 20000
DUTY_MAX = 1023        # MicroPython PWM is 10 bit, not 8

FORWARD = 1
BACKWARD = -1

_motorpwms = {}
_started = False


def _pwm(number):
    """One PWM channel per pin, made once and kept."""
    pwm = _motorpwms.get(number)
    if pwm is None:
        # Frequency and duty given to the constructor rather than set just
        # after it: a bare PWM(Pin(...)) runs at the port's own defaults for
        # the moment in between, which on a motor is a kick and on a servo is
        # a pulse far outside its travel.
        pwm = PWM(Pin(number, Pin.OUT), freq=PWM_FREQ, duty=0)
        _motorpwms[number] = pwm
    return pwm


def motorsbegin():
    """Set the four motor pins up and make sure the robot is stopped."""
    global _started
    for pins_pair in MOTOR_PINS.values():
        for number in pins_pair:
            _pwm(number)
    _started = True
    stoprobot()
    return True


def _ensuremotors():
    if not _started:
        motorsbegin()


# ------------------------------------------------------------ little tools
def speedtoduty(speed):
    """Turn a 0-100 speed into the 0-1023 the PWM wants."""
    speed = int(speed)
    if speed < 0:
        speed = 0
    elif speed > 100:
        speed = 100
    return speed * DUTY_MAX // 100


def _motor(name):
    """'L', 'l', 'left', 'LEFT' -> 'L'."""
    key = str(name).strip().upper()
    if key in MOTOR_PINS:
        return key
    if key in ("LEFT", "1"):
        return "L"
    if key in ("RIGHT", "2"):
        return "R"
    raise ValueError(
        "{} is not a motor. use L for left or R for right".format(repr(name))
    )


def _direction(value):
    """1, -1, 'forward', 'backward', 'f', 'b' -> 1 or -1 (0 means stop)."""
    if isinstance(value, str):
        word = value.strip().lower()
        if word in ("forward", "f", "fwd", "1"):
            return FORWARD
        if word in ("backward", "back", "b", "reverse", "-1"):
            return BACKWARD
        if word in ("stop", "0"):
            return 0
        raise ValueError(
            "{} is not a direction. use FORWARD, BACKWARD, or 1 and "
            "-1".format(repr(value))
        )
    return int(value)


def _apply(motor, duty1, duty2):
    """Put the two duties on a motor's two pins."""
    _ensuremotors()
    in1, in2 = MOTOR_PINS[motor]
    _pwm(in1).duty(duty1)
    _pwm(in2).duty(duty2)


# ------------------------------------------------------- one motor at a time
def motorforward(motor, speed=100):
    """Turn one motor forwards. motorforward("L", 60)"""
    motor = _motor(motor)
    duty = speedtoduty(speed)
    if MOTOR_REVERSED[motor]:
        _apply(motor, 0, duty)
    else:
        _apply(motor, duty, 0)
    return duty


def motorbackward(motor, speed=100):
    """Turn one motor backwards. motorbackward("L", 60)"""
    motor = _motor(motor)
    duty = speedtoduty(speed)
    if MOTOR_REVERSED[motor]:
        _apply(motor, duty, 0)
    else:
        _apply(motor, 0, duty)
    return duty


def leftforward(speed=100):
    """The left wheel, forwards."""
    return motorforward("L", speed)


def leftbackward(speed=100):
    """The left wheel, backwards."""
    return motorbackward("L", speed)


def rightforward(speed=100):
    """The right wheel, forwards."""
    return motorforward("R", speed)


def rightbackward(speed=100):
    """The right wheel, backwards."""
    return motorbackward("R", speed)


def motor(which, speed):
    """One motor with a signed speed: -100 backwards, 0 stop, 100 forwards."""
    which = _motor(which)
    speed = int(speed)
    if speed > 0:
        return motorforward(which, speed)
    if speed < 0:
        return motorbackward(which, -speed)
    stopmotor(which)
    return 0


def leftmotor(speed):
    """The left wheel with a signed speed, -100 to 100."""
    return motor("L", speed)


def rightmotor(speed):
    """The right wheel with a signed speed, -100 to 100."""
    return motor("R", speed)


def drive(left_speed, right_speed):
    """Both wheels at once, each with its own signed speed."""
    motor("L", left_speed)
    motor("R", right_speed)


def runmotor(which, direction, speed):
    """One motor, the same call as the Arduino version."""
    which = _motor(which)
    direction = _direction(direction)
    if direction == FORWARD:
        return motorforward(which, speed)
    if direction == BACKWARD:
        return motorbackward(which, speed)
    stopmotor(which)
    return 0


# --------------------------------------------------------------- both motors
def run(direction, speed):
    """Drive the robot: forward, backward, or spinning left or right.

    Left and right are handled here rather than in _direction() on purpose.
    They are things a PAIR of wheels does -- one forward, one back -- so they
    mean nothing to runmotor(), which drives one wheel and shares that helper.
    """
    if isinstance(direction, str):
        word = direction.strip().lower()
        if word in ("left", "l"):
            left(speed)
            return
        if word in ("right", "r"):
            right(speed)
            return

    direction = _direction(direction)
    if direction == FORWARD:
        leftforward(speed)
        rightforward(speed)
    elif direction == BACKWARD:
        leftbackward(speed)
        rightbackward(speed)
    else:
        stoprobot()


def forward(speed=100):
    """Drive straight forwards."""
    run(FORWARD, speed)


def backward(speed=100):
    """Drive straight backwards."""
    run(BACKWARD, speed)


def left(speed=100):
    """Spin to the left: left wheel back, right wheel forward."""
    leftbackward(speed)
    rightforward(speed)


def right(speed=100):
    """Spin to the right: left wheel forward, right wheel back."""
    leftforward(speed)
    rightbackward(speed)


# ---------------------------------------------------------------- stopping
def stopmotor(which):
    """Let one wheel roll to a stop."""
    _apply(_motor(which), 0, 0)


def stoprobot():
    """Let both wheels roll to a stop."""
    for name in MOTOR_ORDER:
        _apply(name, 0, 0)


def brake(which=None):
    """Stop dead instead of rolling on."""
    names = MOTOR_ORDER if which is None else (_motor(which),)
    for name in names:
        _apply(name, DUTY_MAX, DUTY_MAX)


# ------------------------------------------------------------------- setup
def setmotorpolarity(which, reversed_=True):
    """Say a motor is mounted facing the other way."""
    which = _motor(which)
    MOTOR_REVERSED[which] = bool(reversed_)
    return MOTOR_REVERSED[which]


#: How the board is mounted. Turning it on its side swaps which way the wheels
#: have to turn for the robot to go forwards, so "forward" keeps meaning
#: forward whichever way round it is bolted on.
ORIENTATIONS = ("horizontal", "vertical")

#: The polarity each orientation implies, kept so switching back and forth
#: always lands on the same wiring rather than flipping from wherever it was.
_ORIENTATION_POLARITY = {
    "horizontal": {"L": True, "R": False},
    "vertical": {"L": False, "R": True},
}

ORIENTATION = "horizontal"


def setrobotorientation(orientation="horizontal"):
    """Say which way up the board is mounted, and swap the wheels to suit."""
    global ORIENTATION
    key = str(orientation).strip().lower()
    if key not in _ORIENTATION_POLARITY:
        raise ValueError(
            "unknown orientation '{}'. try one of: {}".format(
                orientation, ", ".join(ORIENTATIONS)
            )
        )
    ORIENTATION = key
    for name, reversed_ in _ORIENTATION_POLARITY[key].items():
        MOTOR_REVERSED[name] = reversed_
    return key


def motorinfo():
    """Print how the motors are wired and set up."""
    for name in MOTOR_ORDER:
        in1, in2 = MOTOR_PINS[name]
        print("   {} motor  gpio {:<3} and {:<3}  {}".format(
            "left " if name == "L" else "right", in1, in2,
            "mounted reversed" if MOTOR_REVERSED[name] else "mounted the usual way"))
    print("   PWM {} Hz, speed 0-100 becomes duty 0-{}".format(
        PWM_FREQ, DUTY_MAX))
    print("   mounted {}".format(ORIENTATION))


# ------------------------------------------------------------------ servos
# A hobby servo is told its angle by how long a pulse it gets, fifty times a
# second: about half a millisecond for one end of its travel and two and a
# half for the other. That is a PWM channel like any other, just a very slow
# one -- and a slow one of its own, so it never shares a timer with the
# motors or the speaker.

SERVO_PINS = {"S1": 16, "S2": 17}
SERVO_ORDER = ("S1", "S2")

SERVO_FREQ = 50           #: fifty pulses a second, what every hobby servo wants
SERVO_MIN_US = 500        #: pulse for 0 degrees
SERVO_MAX_US = 2500       #: pulse for 180 degrees
SERVO_MAX_ANGLE = 180

_servopwms = {}


def _servo(name):
    """'S1', 's1', 'servo 1', '1' -> 'S1'."""
    key = str(name).strip().upper().replace(" ", "").replace("SERVO", "S")
    if key in SERVO_PINS:
        return key
    if key in ("1",):
        return "S1"
    if key in ("2",):
        return "S2"
    raise ValueError(
        "{} is not a servo. use S1 or S2".format(repr(name))
    )


def _servopwm(name):
    """One PWM channel per servo, made on first use and kept."""
    pwm = _servopwms.get(name)
    if pwm is None:
        pwm = PWM(Pin(SERVO_PINS[name], Pin.OUT), freq=SERVO_FREQ, duty_u16=0)
        _servopwms[name] = pwm
    else:
        # Re-asserted every time, not just at creation. The chip shares one
        # timer between channels running at the same frequency, and 50 Hz is
        # inside the speaker's range -- so playing a 50 Hz tone can pull this
        # channel's timer somewhere else, and nothing else would ever put it
        # back.
        pwm.freq(SERVO_FREQ)
    return pwm


def setservo(which, angle):
    """Point a servo at an angle, 0 to 180."""
    name = _servo(which)
    angle = max(0, min(SERVO_MAX_ANGLE, int(angle)))
    span = SERVO_MAX_US - SERVO_MIN_US
    microseconds = SERVO_MIN_US + (span * angle // SERVO_MAX_ANGLE)
    # A full cycle at 50 Hz is 20000 us, and duty_u16 is that cycle as 0-65535.
    _servopwm(name).duty_u16(microseconds * 65535 // 20000)
    return angle


def stopservo(which=None):
    """Stop holding a servo, so it goes limp instead of buzzing."""
    names = SERVO_ORDER if which is None else (_servo(which),)
    for name in names:
        if name in _servopwms:
            _servopwms[name].duty_u16(0)
    return True


def releaseservo(which=None):
    """Give a servo header back, so it can be used as an ordinary pin.

    stopservo only takes the pulse away; the PWM stays on the pin, holding it
    at 50 Hz. That is right for a servo between two moves and wrong for a pin
    about to be driven by something else, because the pin would then have two
    owners and carry neither signal properly. Called by mieo.digitalwrite and
    mieo.setpwm before they touch S1 or S2.
    """
    names = SERVO_ORDER if which is None else (_servo(which),)
    for name in names:
        pwm = _servopwms.pop(name, None)
        if pwm is not None:
            try:
                pwm.deinit()
            except Exception:
                # An already dead PWM is exactly the state we wanted anyway.
                pass
    return True
