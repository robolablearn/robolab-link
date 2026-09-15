"""Mieo: the speaker.

GPIO 25 drives a small amplifier. Every tone here is a square wave from
machine.PWM, which the LED controller generates in hardware -- so a note keeps
sounding accurately while the program gets on with something else, and costs
nothing while it does.

machine.DAC also lives on GPIO 25 and must NEVER be constructed anywhere in
this library. This firmware's DAC has exactly one method, write(0..255), with
no buffered or DMA path and no way to switch the analog output buffer off
again short of a reset -- so it could only make a tone by spinning the CPU at
100%, and once it is on it fights the PWM for the pad for the rest of the run.

Nothing here touches the hardware at import time. mieo.py is imported afresh
on every live-mode handshake, so the PWM channel is built on first use, the
same way the panel is.
"""

import time

import machine
import micropython

#: The speaker. PWM only -- see the note about the DAC above.
SPEAKER_PIN = 25

#: Below this is a thump rather than a note on a speaker this size, and above
#: it a square wave is just a shriek. Anything asked for outside the range is
#: pulled back into it.
#:
#: The floor is 80 rather than 40 on purpose: the servos run at 50 Hz, and the
#: chip shares one timer between channels asking for the same frequency. A
#: range that reached down to 40 could land on 50 and drag the servos' timer
#: with it -- the two would then take turns re-programming each other.
MIN_HZ = 80
MAX_HZ = 5000

#: Live mode gives a statement 15 seconds before it gives up on the board, so
#: a single beep is not allowed to outlast that.
MAX_MS = 10000

#: Quarter note = 500 ms. Every duration below is worked out from this, so
#: changing the tempo is one edit rather than five.
TEMPO_BPM = 120

#: 0-100. Not a volume in decibels: it is how much of each cycle the square
#: wave spends high, which is what a class-D amplifier turns into loudness.
#:
#: Set by ear on the real board, coming down from 60: this amplifier is far
#: more sensitive than a bare speaker would be, so the useful range sits low.
#: Note the curve below is squared, so this is about a sixteenth of the loudest
#: the library will go, not a quarter. Raise it if a future board puts a
#: divider in front of the amp.
VOLUME = 25

#: Timer 0. NOT Timer(-1): on this port that decodes to Timer(3), which the
#: Bluetooth REPL already owns -- taking it would silently break wireless.
_TIMER_ID = 0

#: How often the background melody advances, and how finely a slide is
#: stepped. Fine enough that note edges sound crisp, coarse enough to be
#: invisible next to everything else the board is doing.
_TICK_MS = 10

#: Equal temperament, A4 = 440 Hz, rounded to whole hertz because that is what
#: the hardware takes. The rounding is worth at most about three cents, which
#: nobody can hear.
NOTES = {
    "C3": 131, "C#3": 139, "D3": 147, "D#3": 156, "E3": 165, "F3": 175,
    "F#3": 185, "G3": 196, "G#3": 208, "A3": 220, "A#3": 233, "B3": 247,
    "C4": 262, "C#4": 277, "D4": 294, "D#4": 311, "E4": 330, "F4": 349,
    "F#4": 370, "G4": 392, "G#4": 415, "A4": 440, "A#4": 466, "B4": 494,
    "C5": 523, "C#5": 554, "D5": 587, "D#5": 622, "E5": 659, "F5": 698,
    "F#5": 740, "G5": 784, "G#5": 831, "A5": 880, "A#5": 932, "B5": 988,
    "C6": 1047, "C#6": 1109, "D6": 1175, "D#6": 1245, "E6": 1319, "F6": 1397,
    "F#6": 1480, "G6": 1568, "G#6": 1661, "A6": 1760, "A#6": 1865, "B6": 1976,
    "C7": 2093,
}

#: A flat is the sharp below it. Written out so a student who types "Bb"
#: because that is what the sheet music says gets a note rather than an error.
_FLATS = {"DB": "C#", "EB": "D#", "GB": "F#", "AB": "G#", "BB": "A#"}

_BEAT_MS = 60000 // TEMPO_BPM

DURATIONS = {
    "Whole": _BEAT_MS * 4,
    "Half": _BEAT_MS * 2,
    "Quarter": _BEAT_MS,
    "Eighth": _BEAT_MS // 2,
    "Sixteenth": _BEAT_MS // 4,
}

#: Every sound is a run of (from_hz, to_hz, milliseconds) pieces. The two
#: frequencies being equal is a steady note; different, and the pitch slides
#: between them; both zero is a rest. One shape for every case means the
#: player never has to ask what kind of piece it is looking at.
SOUNDS = {
    "beep": ((1047, 1047, 120),),
    "chirp": ((2000, 3200, 60), (0, 0, 40), (2400, 3600, 50), (0, 0, 30),
              (2800, 3900, 45)),
    "coin": ((988, 988, 90), (1319, 1319, 480)),
    "win": ((523, 523, 120), (659, 659, 120), (784, 784, 120), (0, 0, 40),
            (1047, 1047, 420)),
    "lose": ((392, 392, 150), (370, 370, 150), (349, 349, 150),
             (330, 330, 300), (330, 165, 300)),
    "sad": ((440, 440, 300), (0, 0, 30), (349, 349, 300), (0, 0, 30),
            (294, 294, 520)),
    "happy": ((659, 659, 110), (784, 784, 110), (1319, 1319, 110),
              (1047, 1047, 110), (1175, 1175, 110), (1568, 1568, 260)),
    "sleep": ((150, 330, 350), (0, 0, 140), (330, 150, 350)),
    "startup": ((220, 880, 220), (0, 0, 40), (523, 523, 100), (784, 784, 260)),
    "alarm": ((880, 880, 150), (1245, 1245, 150), (880, 880, 150),
              (1245, 1245, 150), (880, 880, 150), (1245, 1245, 150),
              (880, 880, 150), (1245, 1245, 150)),
    "laser": ((3000, 300, 260),),
    "error": ((294, 294, 180), (0, 0, 60), (208, 208, 360)),
}

_pwm = None
_timer = None
_queue = None
_piece = None
_piece_ends = 0
_pending = False


# ----------------------------------------------------------------- the pin

def _speaker():
    """The PWM channel, made on first use and kept for the whole program."""
    global _pwm
    if _pwm is None:
        _pwm = machine.PWM(machine.Pin(SPEAKER_PIN), freq=1000, duty_u16=0)
    return _pwm


def _duty():
    """VOLUME as a duty cycle.

    Squared, because loudness is nearer the square of amplitude than the
    amplitude itself, so a straight mapping puts almost all the useful range
    in the bottom third of the slider. Half a cycle is the loudest a square
    wave gets; nothing here ever asks for more.
    """
    level = max(0, min(100, VOLUME)) / 100.0
    return int(65535 * 0.5 * level * level)


def _tone(hz):
    """Start (or move to) a pitch and leave it sounding."""
    speaker = _speaker()
    speaker.freq(int(max(MIN_HZ, min(MAX_HZ, hz))))
    speaker.duty_u16(_duty())


def _silence():
    """Stop making a noise.

    The channel is left running at zero duty rather than shut down: that parks
    the pin at a steady low, which is silent and cannot pop, and it means the
    next note starts instantly instead of rebuilding the channel.
    """
    if _pwm is not None:
        _pwm.duty_u16(0)


def setvolume(level):
    """How loud, 0-100. Takes effect from the next note."""
    global VOLUME
    VOLUME = max(0, min(100, int(level)))
    if _pwm is not None and _piece is not None:
        _pwm.duty_u16(_duty())
    return VOLUME


def volume():
    return VOLUME


# ------------------------------------------------------------- looking up

def notefreq(name):
    """Hertz for a note like "C4", "F#5" or "Bb3"."""
    key = str(name).strip().upper().replace(" ", "")
    if key in NOTES:
        return NOTES[key]
    # Try it as a flat: "BB3" -> "A#3".
    if len(key) > 2 and key[:2] in _FLATS:
        moved = _FLATS[key[:2]] + key[2:]
        if moved in NOTES:
            return NOTES[moved]
    raise ValueError(
        "unknown note '{}'. notes run from C3 to C7, like C4 or F#5".format(name)
    )


def durationms(name):
    """Milliseconds for a duration like "Eighth".

    Matched without regard to case, by hand: MicroPython has no
    str.capitalize(), so the obvious one-liner works on a desktop and fails on
    the board.
    """
    key = str(name).strip().lower()
    for known in DURATIONS:
        if known.lower() == key:
            return DURATIONS[known]
    raise ValueError(
        "unknown duration '{}'. try one of: {}".format(
            name, ", ".join(sorted(DURATIONS))
        )
    )


def _pieces(name):
    key = str(name).strip().lower()
    if key not in SOUNDS:
        raise ValueError(
            "unknown sound '{}'. try one of: {}".format(name, ", ".join(sorted(SOUNDS)))
        )
    return SOUNDS[key]


def _slide(start, end, done):
    """The pitch part way through a slide, `done` being 0.0 to 1.0.

    Geometric rather than straight-line: pitch is heard in ratios, so an even
    slide from 300 Hz to 3000 Hz has to multiply, not add, or the whole sweep
    sounds bunched up at the top.
    """
    return start * ((float(end) / float(start)) ** done)


# ------------------------------------------------- playing in the foreground

def _playpiece(start, end, ms):
    if ms <= 0:
        return
    if start <= 0 and end <= 0:
        _silence()
        time.sleep_ms(ms)
        return
    if start == end:
        _tone(start)
        time.sleep_ms(ms)
        return

    steps = max(1, ms // _TICK_MS)
    for step in range(steps):
        _tone(_slide(start, end, float(step) / steps))
        time.sleep_ms(_TICK_MS)


def _walk(pieces):
    """Play the whole thing and only come back when it has finished.

    The finally is what makes Ctrl-C safe: stopping a program part way through
    a melody must not leave the speaker howling.
    """
    try:
        for start, end, ms in pieces:
            _playpiece(start, end, ms)
    finally:
        _silence()


def playfreq(hz, ms):
    """Hold one frequency for a while, then stop. Waits for it to finish.

    A frequency of zero or less is a rest rather than an error: asking for
    silence is a reasonable thing for a program to do.
    """
    hz = int(hz)
    ms = max(0, min(MAX_MS, int(ms)))
    stopsound()
    if hz <= 0:
        _silence()
        time.sleep_ms(ms)
        return True
    _walk(((hz, hz, ms),))
    return True


def playtone(note, duration):
    """Play a named note for a named length. Waits for it to finish."""
    hz = notefreq(note)
    ms = durationms(duration)
    stopsound()
    _walk(((hz, hz, ms),))
    return True


def playsounduntildone(name):
    """Play a built-in sound and wait for the end of it."""
    pieces = _pieces(name)
    stopsound()
    _walk(pieces)
    return True


# ------------------------------------------------- playing in the background

def _advance(_arg):
    """Move the background melody along. Runs from the scheduler.

    Kept deliberately small and incapable of raising: it runs between whatever
    the program itself is doing, and an exception here would surface in the
    middle of somebody else's code.
    """
    global _pending, _queue, _piece, _piece_ends
    _pending = False

    try:
        if _queue is None:
            return

        now = time.ticks_ms()
        if _piece is not None and time.ticks_diff(_piece_ends, now) > 0:
            start, end, ms = _piece
            if start != end and start > 0 and end > 0 and ms > 0:
                left = time.ticks_diff(_piece_ends, now)
                _tone(_slide(start, end, float(ms - left) / ms))
            return

        if not _queue:
            _queue = None
            _piece = None
            _silence()
            return

        _piece = _queue.pop(0)
        start, end, ms = _piece
        _piece_ends = time.ticks_add(now, ms)
        if start <= 0 and end <= 0:
            _silence()
        else:
            _tone(start)
    except Exception:
        _queue = None
        _piece = None
        try:
            _silence()
        except Exception:
            pass


def _tick(_timer_arg):
    """The timer's callback. Hands the real work to the scheduler.

    It does nothing itself on purpose. This callback must never re-arm its own
    timer -- that is the pattern that has crashed this board before -- and the
    single pending flag keeps the scheduler queue from filling up if the
    program is too busy to service it for a moment.
    """
    global _pending
    if _pending:
        return
    _pending = True
    try:
        micropython.schedule(_advance, 0)
    except RuntimeError:
        _pending = False


def playsound(name):
    """Start a built-in sound and carry straight on.

    There is one speaker, so there is one voice: starting a sound replaces
    whatever was playing rather than layering on top of it.
    """
    global _queue, _piece, _timer

    pieces = _pieces(name)
    stopsound()

    _queue = list(pieces)
    _piece = None
    if _timer is None:
        # Armed once, for the life of the program, and never re-armed.
        _timer = machine.Timer(_TIMER_ID)
        _timer.init(period=_TICK_MS, mode=machine.Timer.PERIODIC, callback=_tick)
    return True


def stopsound():
    """Stop anything playing and forget the rest of it."""
    global _queue, _piece
    _queue = None
    _piece = None
    _silence()
    return True


def isplaying():
    """Whether a background sound is still going."""
    return _queue is not None
