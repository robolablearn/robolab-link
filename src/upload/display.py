"""The display: the LED panel, the faces, and the animations.

Three things that always travel together, so they live in one file:

    LedMatrix      the panel itself -- x/y -> LED number, brightness
    the faces      12 eye faces, 23 colours to draw them with
    the animations one moving version of every face
    the font       A-Z a-z 0-9 and symbols, and scrolling a message
    the font      letters and numbers, and scrolling them past

The panel is wired to gpio 2 and that cannot be changed -- it is soldered
to the board, so PANEL_PIN is a fact rather than a setting.

Brightness
----------
Every LED at once is what makes the regulator warm, so the panel is limited
to BRIGHTNESS_LIMIT (40%) of what the LEDs could actually do.  The library
still talks in 0-100, and 100 now means 40% of full power rather than full.

Within that scale the panel starts at DEFAULT_LEVEL (30), which works out at
12% of what the LEDs could really do.  A panel this close to the eyes is
easier to look at dim than bright, and anything that wants more can simply
ask for it.

Both numbers are enforced rather than merely intended: every colour leaves
through LedMatrix.show(), which scales by self.brightness, and the only two
places that value is written -- the constructor and set_brightness() -- clamp
to BRIGHTNESS_LIMIT.  So no face, animation or pattern can drive the panel
past 40%, including the ones that pulse brightness above their own baseline.
"""

import time
import random

import machine
import micropython
from machine import Pin
from neopixel import NeoPixel

#: the panel is soldered to gpio 2 on this board. it is not a setting --
#: nothing else may use gpio 2, and the panel is never anywhere else.
PANEL_PIN = 2

#: how many LEDs, and how they are arranged
WIDTH = 7
HEIGHT = 5

#: the most the LEDs are ever driven to, whatever anyone asks for.
#: 35 LEDs at full white pull about 2 A, and it is the regulator rather than
#: the LEDs that gets hot, so the ceiling is 40% of full power.  The 0-100 the
#: library talks in is mapped onto 0 to this, by scalebrightness() below.
#: Every colour leaves through LedMatrix.show(), which scales by
#: self.brightness, and set_brightness() clamps to this -- so no drawing
#: function, animation or pattern can drive the panel past it.
BRIGHTNESS_LIMIT = 0.40

#: where the panel starts, on the same 0-100 scale the blocks use.
DEFAULT_LEVEL = 30

#: the same figure as a fraction of full power, which is what the panel is
#: actually driven to.  Worked out from the two numbers above rather than
#: written down separately, so raising or lowering the ceiling keeps the
#: default at the same place on the scale a student sees.
DEFAULT_BRIGHTNESS = BRIGHTNESS_LIMIT * DEFAULT_LEVEL / 100.0


def scalebrightness(value):
    """Turn a 0-100 brightness into what the panel is really driven to.

        scalebrightness(100) -> 0.40    as bright as the board allows
        scalebrightness(30)  -> 0.12    where the panel starts
        scalebrightness(0)   -> 0.0     off

    100 does not mean full power: it means BRIGHTNESS_LIMIT of it.  A value
    already between 0.0 and 1.0 is treated as the same fraction.
    """
    if value > 1:
        value = value / 100.0
    return max(0.0, min(1.0, value)) * BRIGHTNESS_LIMIT



# ------------------------------------------------------------ the panel

class LedMatrix:
    def __init__(self, pin=None, width=WIDTH, height=HEIGHT,
                 brightness=DEFAULT_BRIGHTNESS,
                 serpentine=False, reverse_first_row=False,
                 flip_x=False, flip_y=False):
        """The panel. There is no pin to choose -- it is soldered to gpio 2.

        `pin` is still the first argument so older code calling
        LedMatrix(2, ...) keeps working; any other number is refused rather
        than quietly taken as the width.

        serpentine          zigzag wiring: every other row runs backwards.
                            The panel is wired straight through -- LED n is
                            row n // 7, column n % 7, the same mapping the
                            Arduino firmware uses (row * 7 + col) -- so this
                            is off. Turning it on makes every second row come
                            out mirrored, which reads as the picture snaking.
        reverse_first_row   which rows are the backwards ones
        flip_x / flip_y     mirror the picture left/right or top/bottom
        """
        if pin is not None and pin != PANEL_PIN:
            raise ValueError(
                "the panel is soldered to gpio {}, so gpio {} cannot drive "
                "it".format(PANEL_PIN, pin))
        self.width = width
        self.height = height
        self.n = width * height
        # Through the same clamp as set_brightness: constructing the panel must
        # not be a way around the ceiling either.
        self.brightness = max(0.0, min(BRIGHTNESS_LIMIT, brightness))
        self.serpentine = serpentine
        self.reverse_first_row = reverse_first_row
        self.flip_x = flip_x
        self.flip_y = flip_y
        self._pin = Pin(PANEL_PIN, Pin.OUT)
        self._np = NeoPixel(self._pin, self.n)
        # unscaled colors are kept separately so brightness changes don't lose data
        self._buf = [(0, 0, 0)] * self.n
        self._map = []
        self._buildmap()

    def set_wiring(self, serpentine=None, reverse_first_row=None,
                   flip_x=None, flip_y=None):
        """Change the wiring options without rebuilding the panel."""
        if serpentine is not None:
            self.serpentine = serpentine
        if reverse_first_row is not None:
            self.reverse_first_row = reverse_first_row
        if flip_x is not None:
            self.flip_x = flip_x
        if flip_y is not None:
            self.flip_y = flip_y
        self._buildmap()

    def _buildmap(self):
        """Work out x/y -> LED number once, instead of on every pixel."""
        self._map = []
        for y in range(self.height):
            for x in range(self.width):
                col, row = x, y
                if self.flip_x:
                    col = self.width - 1 - col
                if self.flip_y:
                    row = self.height - 1 - row
                if self.serpentine:
                    # one row runs left to right, the next runs right to
                    # left. reverse_first_row picks which starts backwards.
                    backwards = ((row % 2 == 0) if self.reverse_first_row
                                 else (row % 2 == 1))
                    if backwards:
                        col = self.width - 1 - col
                self._map.append(row * self.width + col)

    def _index(self, x, y):
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise IndexError("pixel ({}, {}) out of range".format(x, y))
        return self._map[y * self.width + x]

    def set_pixel(self, x, y, color):
        self._buf[self._map[y * self.width + x]] = color

    def set_led(self, i, color):
        """Light LED number `i` in the order they are wired up, ignoring x/y."""
        if not (0 <= i < self.n):
            raise IndexError("led {} out of range (0-{})".format(i, self.n - 1))
        self._buf[i] = color

    def set_brightness(self, brightness):
        """Set the brightness, 0.0 to BRIGHTNESS_LIMIT.

        Anything higher is pulled back to the limit, so no code path -- not
        even an animation winding the brightness up for a heartbeat -- can
        drive the LEDs hot.
        """
        self.brightness = max(0.0, min(BRIGHTNESS_LIMIT, brightness))

    def clear(self):
        """Blank the picture, without building a new list to do it.

        Scrolling text clears once per column, so making a fresh 35 item
        list every time is 35 objects of rubbish per column.
        """
        buf = self._buf
        off = (0, 0, 0)
        for i in range(self.n):
            buf[i] = off

    def fill(self, color):
        """Every LED the same colour, in place."""
        buf = self._buf
        for i in range(self.n):
            buf[i] = color

    def draw_face(self, bitmap, palette, bg=(0, 0, 0)):
        """bitmap: list of `height` strings, each `width` chars."""
        buf = self._buf
        wire = self._map
        width = self.width
        for y, row in enumerate(bitmap):
            base = y * width
            for x, ch in enumerate(row):
                buf[wire[base + x]] = palette.get(ch, bg)

    def show(self):
        """Push the picture out to the LEDs."""
        b = int(self.brightness * 10000 + 0.5)
        np = self._np
        if b >= 10000:                      # full brightness, nothing to scale
            for i, colour in enumerate(self._buf):
                np[i] = colour
        elif b <= 0:
            for i in range(self.n):
                np[i] = (0, 0, 0)
        else:
            for i, (r, g, bl) in enumerate(self._buf):
                np[i] = (r * b // 10000, g * b // 10000, bl * b // 10000)
        np.write()



# ------------------------------------------------- the colours and faces

# ---------------------------------------------------------------- colours
# the originals
RED = (255, 0, 0)
ORANGE = (255, 90, 0)
YELLOW = (255, 200, 0)
GREEN = (0, 255, 0)
CYAN = (0, 200, 255)
BLUE = (0, 60, 255)
PURPLE = (140, 0, 255)
MAGENTA = (255, 0, 200)
WHITE = (255, 255, 255)
PINK = (255, 80, 120)
OFF = (0, 0, 0)

# more to draw with
AMBER = (255, 150, 0)      # warm gold, between orange and yellow
PEACH = (255, 170, 120)    # pale warm skin tone
CORAL = (255, 70, 60)      # soft warm red
LIME = (170, 255, 0)       # sharp yellow green
FOREST = (0, 100, 30)      # deep dark green
AQUA = (0, 255, 180)       # green side of cyan
TEAL = (0, 140, 140)       # dark cyan
SKY = (80, 170, 255)       # pale daylight blue
NAVY = (0, 0, 140)         # deep dark blue
INDIGO = (75, 0, 200)      # blue side of purple
VIOLET = (200, 60, 255)    # light purple
GREY = (120, 120, 120)     # dim white
DIM = (40, 40, 40)         # barely on, good for outlines and shadows

PALETTE = {
    "R": RED,
    "O": ORANGE,
    "A": AMBER,
    "Y": YELLOW,
    "H": PEACH,
    "F": CORAL,
    "L": LIME,
    "G": GREEN,
    "J": FOREST,
    "Q": AQUA,
    "T": TEAL,
    "C": CYAN,
    "S": SKY,
    "B": BLUE,
    "N": NAVY,
    "I": INDIGO,
    "V": VIOLET,
    "P": PURPLE,
    "M": MAGENTA,
    "K": PINK,
    "W": WHITE,
    "E": GREY,
    "D": DIM,
}

COLORS = {
    "red": RED,
    "orange": ORANGE,
    "amber": AMBER,
    "yellow": YELLOW,
    "peach": PEACH,
    "coral": CORAL,
    "lime": LIME,
    "green": GREEN,
    "forest": FOREST,
    "aqua": AQUA,
    "teal": TEAL,
    "cyan": CYAN,
    "sky": SKY,
    "blue": BLUE,
    "navy": NAVY,
    "indigo": INDIGO,
    "violet": VIOLET,
    "purple": PURPLE,
    "magenta": MAGENTA,
    "pink": PINK,
    "white": WHITE,
    "grey": GREY,
    "gray": GREY,
    "dim": DIM,
    "off": OFF,
}

# the palette in a sensible order, for colour charts and drop down menus.
# (a plain list because MicroPython dictionaries do not keep their order)
PALETTE_ORDER = (
    ("R", "red"), ("O", "orange"), ("A", "amber"), ("Y", "yellow"),
    ("H", "peach"), ("F", "coral"), ("L", "lime"), ("G", "green"),
    ("J", "forest"), ("Q", "aqua"), ("T", "teal"), ("C", "cyan"),
    ("S", "sky"), ("B", "blue"), ("N", "navy"), ("I", "indigo"),
    ("V", "violet"), ("P", "purple"), ("M", "magenta"), ("K", "pink"),
    ("W", "white"), ("E", "grey"), ("D", "dim"),
)


def from_bits(rows, color="Y", width=WIDTH):
    """Turn Arduino style rows (0b0100010 ...) into a colour bitmap."""
    out = []
    for row in rows:
        out.append(
            "".join(color if (row >> (width - 1 - x)) & 1 else "." for x in range(width))
        )
    return out


def to_bits(bitmap):
    """Turn a colour bitmap back into Arduino style rows."""
    rows = []
    for line in bitmap:
        value = 0
        for x, ch in enumerate(line):
            if ch != ".":
                value |= 1 << (len(line) - 1 - x)
        rows.append(value)
    return tuple(rows)


# ------------------------------------------------------------------ faces
HAPPY = [            # aqua lid arched over a yellow eye
    ".QQ.QQ.",
    "Q..Q..Q",
    ".YY.YY.",
    ".YY.YY.",
    ".......",
]

NEUTRAL = [          # blue brow over a big red eye
    "BBB.BBB",
    ".......",
    "RRR.RRR",
    "RBR.RBR",
    "RRR.RRR",
]

SHY = [              # round white eyes with a pink pupil
    ".......",
    ".W...W.",
    "WKW.WKW",
    ".W...W.",
    ".......",
]

CRY = [              # coral eyes with sky blue tears streaming down
    ".......",
    "FFF.FFF",
    "FSF.FSF",
    ".S...S.",
    ".S...S.",
]

ANGRY = [            # brows driven down into narrowed eyes
    ".......",
    "RR...RR",
    "..R.R..",
    "OOO.OOO",
    ".O...O.",
]

SURPRISE = [         # two big round eyes, magenta lid over purple
    ".MM.MM.",
    "M..M..M",
    "P..P..P",
    ".PP.PP.",
    ".......",
]

THINK = [            # thought bubble drifting off, eyes glancing up
    ".....BB",
    "BBB.B..",
    ".O...O.",
    ".O...O.",
    ".......",
]

SLEEP = [            # lids shut (the z only turns up in the animation)
    ".......",
    ".......",
    "CCC.CCC",
    ".......",
    ".......",
]

NERD = [             # navy specs, hollow lenses, bridge across the nose
    ".......",
    "NNN.NNN",
    "N.NNN.N",
    "NNN.NNN",
    ".......",
]

DUDE = [             # shades, one bar across the top
    ".......",
    ".BBBBB.",
    "BBB.BBB",
    ".BB.BB.",
    ".......",
]

HEART = [            # one big heart filling the panel
    ".RR.RR.",
    "RRRRRRR",
    ".RRRRR.",
    "..RRR..",
    "...R...",
]

DISCO = [            # not a face -- 19 of the palette colours at once
    ".......",
    "ABCDEFG",
    "HIJ.KLM",
    "NOP.QRS",
    ".......",
]

FACES = {
    "happy": HAPPY,
    "neutral": NEUTRAL,
    "shy": SHY,
    "cry": CRY,
    "angry": ANGRY,
    "surprise": SURPRISE,
    "think": THINK,
    "sleep": SLEEP,
    "nerd": NERD,
    "dude": DUDE,
    "heart": HEART,
    "disco": DISCO,
}

# demo / menu order (nicer than alphabetical for students)
NAMES = [
    "happy",
    "neutral",
    "shy",
    "cry",
    "angry",
    "surprise",
    "think",
    "sleep",
    "nerd",
    "dude",
    "heart",
    "disco",
]

# other words students are likely to type
ALIASES = {
    "smile": "happy",
    "smiley": "happy",
    "glad": "happy",
    "blush": "shy",
    "crying": "cry",
    "tears": "cry",
    "sad": "cry",
    "mad": "angry",
    "cross": "angry",
    "wow": "surprise",
    "shock": "surprise",
    "shocked": "surprise",
    "omg": "surprise",
    "thinking": "think",
    "idea": "think",
    "sleepy": "sleep",
    "zzz": "sleep",
    "geek": "nerd",
    "glasses": "nerd",
    "cool": "dude",
    "sunglasses": "dude",
    "love": "heart",
    "inlove": "heart",
    "party": "disco",
    "rainbow": "disco",
    "colours": "disco",
    "colors": "disco",
}


def resolveemotion(name):
    """Tidy up a name typed by a student: 'Happy ' and 'smile' -> 'happy'."""
    key = str(name).strip().lower()
    return ALIASES.get(key, key)


def getface(name):
    """Return the bitmap for `name`, with a helpful error if it is unknown."""
    key = resolveemotion(name)
    try:
        return FACES[key]
    except KeyError:
        raise ValueError(
            "unknown emotion '{}'. try one of: {}".format(name, ", ".join(NAMES))
        )


#: The block editor's painter palette. Index 0 is "LED off"; a painted cell
#: stores its palette index as one character, so a whole 7x5 picture is 35
#: characters of plain digits. Must stay in step with
#: Blockly.FieldMatrixColor.PALETTE in openblock-blocks -- the editor and the
#: panel have to agree on what "3" means.
PAINTER_PALETTE = (
    (0, 0, 0),        # 0  off
    (255, 255, 255),  # 1  white
    (255, 200, 0),    # 2  yellow
    (255, 0, 0),      # 3  red
    (255, 0, 255),    # 4  magenta
    (0, 255, 255),    # 5  cyan
    (0, 0, 255),      # 6  blue
    (0, 255, 0),      # 7  green
    (255, 140, 0),    # 8  orange
    (0, 160, 0),      # 9  dark green
)


def drawpainted(matrix, pattern):
    """Draw a picture where every pixel carries its own colour.

    `pattern` is WIDTH*HEIGHT characters, row by row from the top left, each
    one an index into PAINTER_PALETTE. Anything unrecognised is treated as off,
    so a pattern from an older on/off painter still draws -- its "1"s simply
    come out as palette colour 1.
    """
    text = str(pattern)
    cells = matrix.width * matrix.height
    if len(text) < cells:
        text += "0" * (cells - len(text))
    palette = PAINTER_PALETTE
    for y in range(matrix.height):
        row = y * matrix.width
        for x in range(matrix.width):
            ch = text[row + x]
            index = ord(ch) - 48 if "0" <= ch <= "9" else 0
            matrix.set_pixel(x, y, palette[index])
    matrix.show()


def drawpattern(matrix, pattern, color=None, bg=(0, 0, 0)):
    """Draw a painted on/off pattern.

    `pattern` is WIDTH*HEIGHT characters, row by row from the top left --
    exactly the order the block editor's matrix field stores them in. Anything
    other than "0" lights the pixel, so "1" and "#" both work.

    Short patterns are padded with blanks and long ones are trimmed, so a
    pattern painted for a different sized panel still draws something instead
    of raising.
    """
    text = str(pattern)
    cells = matrix.width * matrix.height
    if len(text) < cells:
        text += "0" * (cells - len(text))
    lit = color if color is not None else YELLOW
    for y in range(matrix.height):
        row = y * matrix.width
        for x in range(matrix.width):
            matrix.set_pixel(x, y, bg if text[row + x] == "0" else lit)
    matrix.show()


def tocolor(value, default=None):
    """Turn whatever a block handed us into an (r, g, b) tuple.

    Accepts "#RRGGBB" from the colour picker, an english colour name, or an
    (r, g, b) tuple that is already a colour.
    """
    if value is None:
        return default if default is not None else YELLOW
    if isinstance(value, (tuple, list)):
        return tuple(value)
    text = str(value).strip()
    if text.startswith("#"):
        digits = text[1:]
        if len(digits) == 6:
            try:
                return (int(digits[0:2], 16),
                        int(digits[2:4], 16),
                        int(digits[4:6], 16))
            except ValueError:
                pass
        raise ValueError("'{}' is not a colour like #RRGGBB".format(text))
    return getcolor(text)


def getcolor(name):
    """Look up a colour by its english name, e.g. color('red')."""
    try:
        return COLORS[str(name).strip().lower()]
    except KeyError:
        raise ValueError(
            "unknown colour '{}'. try one of: {}".format(
                name, ", ".join(sorted(COLORS))
            )
        )



# -------------------------------------------------------- the animations

FRAME_DELAY = 0.12


# ------------------------------------------------------------- basic draw
def _solid_palette(color):
    """A palette where every colour letter becomes the same colour."""
    return {ch: color for ch in PALETTE}


def drawface(matrix, bitmap, color=None):
    """Draw one bitmap. `color` forces the whole face to a single colour."""
    palette = PALETTE if color is None else _solid_palette(color)
    matrix.draw_face(bitmap, palette)
    matrix.show()


def _showface(matrix, name, color=None):
    """Show a face. It stays on screen until something else is drawn."""
    drawface(matrix, getface(name), color)


def _drain(steps):
    """Run a step generator to the end here and now, sleeping between frames.

    This is what makes an "until done" block wait. The animation itself lives
    in the generator, so the blocking form and the background form are the
    same frames at the same speed -- only the thing driving them differs.
    """
    for delay in steps:
        if delay:
            time.sleep(delay)
    return True


def _playframessteps(matrix, frames, color=None, repeat=3, delay=FRAME_DELAY):
    """A list of bitmaps, a frame at a time."""
    for _ in range(repeat):
        for frame in frames:
            drawface(matrix, frame, color)
            yield delay


def playframes(matrix, frames, color=None, repeat=3, delay=FRAME_DELAY):
    """Play a list of bitmaps as an animation."""
    return _drain(_playframessteps(matrix, frames, color, repeat, delay))


def shift(bitmap, dx=0, dy=0, bg="."):
    """Move a bitmap by dx/dy pixels. Anything pushed off the edge is lost."""
    height = len(bitmap)
    width = len(bitmap[0])
    blank = bg * width
    out = []
    for y in range(height):
        src = y - dy
        if not (0 <= src < height):
            out.append(blank)
            continue
        row = bitmap[src]
        if dx > 0:
            row = bg * dx + row[: width - dx]
        elif dx < 0:
            row = row[-dx:] + bg * (-dx)
        out.append(row)
    return out


def _replace(bitmap, y, row):
    """Copy of `bitmap` with one row swapped out."""
    new = list(bitmap)
    new[y] = row
    return new


def blink_frame(color="W"):
    """The eyes shut: one flat lid across the middle."""
    lid = color * 3 + "." + color * 3
    return [".......", ".......", lid, ".......", "......."]


# ---------------------------------------------------------- frame recipes
# the lid arch bouncing with the good mood
HAPPY_FRAMES = [
    HAPPY,
    HAPPY,
    shift(HAPPY, dy=1),
]

# eyes looking around: the pupil moves inside the eye. row 3 is the pupil
# row of NEUTRAL, and B is the pupil sitting in an R eye.
_LOOK = {
    "left": "BRR.BRR",
    "middle": "RBR.RBR",
    "right": "RRB.RRB",
}

NEUTRAL_FRAMES = [
    NEUTRAL,
    NEUTRAL,
    _replace(NEUTRAL, 3, _LOOK["left"]),
    _replace(NEUTRAL, 3, _LOOK["middle"]),
    _replace(NEUTRAL, 3, _LOOK["right"]),
    _replace(NEUTRAL, 3, _LOOK["middle"]),
    blink_frame("R"),
]

# glancing away, then a bashful blink.  row 2 is the pupil row: K sits in
# the middle of the white ring, and slides to one side to look away.
SHY_FRAMES = [
    SHY,
    SHY,
    _replace(SHY, 2, "WWK.WWK"),
    _replace(SHY, 2, "WWK.WWK"),
    blink_frame("W"),
]

# a drop swelling at the eye, then running down and away
CRY_FRAMES = [
    _replace(_replace(CRY, 3, ".S...S."), 4, "......."),
    CRY,
    _replace(CRY, 3, "......."),
    _replace(_replace(CRY, 3, "......."), 4, "......."),
]

# eyes popping open, small to huge
SURPRISE_FRAMES = [
    [".......", ".......", ".MM.MM.", ".......", "......."],
    [".......", ".MM.MM.", "M..M..M", ".PP.PP.", "......."],
    SURPRISE,
    SURPRISE,
]

# the thought bubble drifting up out of nowhere
THINK_FRAMES = [
    _replace(_replace(THINK, 0, "......."), 1, "......."),
    _replace(THINK, 0, "......."),
    THINK,
    THINK,
]

# a z drifting up and away. the face itself is just the two shut lids, so
# the z is only ever part of the animation -- in the same cyan as the lids.
SLEEP_FRAMES = [
    SLEEP,
    _replace(SLEEP, 0, "......C"),
    _replace(SLEEP, 0, ".....C."),
    _replace(SLEEP, 0, "....C.."),
]

# light running across the lenses
NERD_FRAMES = [
    NERD,
    _replace(NERD, 1, "WNN.NNN"),
    _replace(NERD, 1, "NNW.NNN"),
    _replace(NERD, 1, "NNN.WNN"),
    _replace(NERD, 1, "NNN.NNW"),
]

DISCO_COLORS = [
    RED,
    ORANGE,
    YELLOW,
    GREEN,
    CYAN,
    BLUE,
    PURPLE,
    MAGENTA,
]


# --------------------------------------------------------------- specials

#: The brightness an animation is swinging around, and will put back when it
#: ends. Shared rather than kept in the animation, so that setbrightness can
#: move it: the panel is running in the background now, and a level set during
#: a heartbeat has to survive the end of that heartbeat.
_anim_base = None


def _holdbrightness(matrix):
    """Remember the level to swing around, before an animation starts."""
    global _anim_base
    _anim_base = matrix.brightness


def _basebrightness():
    """The level to swing around right now."""
    return DEFAULT_BRIGHTNESS if _anim_base is None else _anim_base


def _releasebrightness(matrix, show=True):
    """Put the level back at the end of an animation, or when it is stopped.

    The faces redraw themselves on the way out so the last frame is not left
    dimmed. The breathe pattern deliberately does not: it is about to be
    blanked anyway, and redrawing would put an extra frame on the panel that
    was never there before.
    """
    global _anim_base
    if _anim_base is not None:
        matrix.set_brightness(_anim_base)
        _anim_base = None
    if show:
        matrix.show()


def setpanelbrightness(matrix, value):
    """Set the brightness, and make it stick even mid animation.

    Without moving the shared level too, an animation that is pulsing would
    put the old brightness back when it ended and the new setting would
    vanish a second after it was asked for.
    """
    global _anim_base
    matrix.set_brightness(value)
    if _anim_base is not None:
        _anim_base = matrix.brightness
    return matrix.brightness


def _beatsteps(matrix, bitmap, color=None, beats=3):
    """Draw something once, then thump it with the brightness knob.

    The finally is what puts the brightness back if the animation is stopped
    part way through: closing a generator raises GeneratorExit at the yield,
    so this runs whether it ended on its own or somebody took the panel.
    """
    _holdbrightness(matrix)
    drawface(matrix, bitmap, color)
    try:
        for _ in range(beats):
            # The peak is the panel's own level, never above it, so a
            # heartbeat is no brighter than the brightness that was set.
            for level in (0.21, 0.71, 0.36, 1.0, 0.43, 0.21):
                matrix.set_brightness(_basebrightness() * level)
                matrix.show()
                yield 0.07
    finally:
        _releasebrightness(matrix)


def _beat(matrix, bitmap, color=None, beats=3):
    return _drain(_beatsteps(matrix, bitmap, color, beats))


def _heartbeatsteps(matrix, color=None, beats=3):
    """The big heart, beating."""
    yield from _beatsteps(matrix, HEART, color, beats)


def _heartbeat(matrix, color=None, beats=3):
    return _drain(_heartbeatsteps(matrix, color, beats))


def _discosparklesteps(matrix, color=None, seconds=3.0, delay=0.08, sparkles=10):
    """Random coloured sparkles all over the panel."""
    try:
        for _ in range(int(seconds / delay)):
            matrix.clear()
            for _ in range(sparkles):
                x = random.randint(0, matrix.width - 1)
                y = random.randint(0, matrix.height - 1)
                matrix.set_pixel(x, y, color or random.choice(DISCO_COLORS))
            matrix.show()
            yield delay
    finally:
        matrix.clear()
        matrix.show()


def _discosparkle(matrix, color=None, seconds=3.0, delay=0.08, sparkles=10):
    return _drain(_discosparklesteps(matrix, color, seconds, delay, sparkles))


def _angryshakesteps(matrix, color=None, shakes=3):
    """Shake the eyes left and right, then flash red."""
    face = ANGRY
    for _ in range(shakes):
        for dx in (-1, 0, 1, 0):
            drawface(matrix, shift(face, dx=dx), color)
            yield 0.06
    for _ in range(2):
        matrix.fill(color or RED)
        matrix.show()
        yield 0.06
        drawface(matrix, face, color)
        yield 0.12


def _angryshake(matrix, color=None, shakes=3):
    return _drain(_angryshakesteps(matrix, color, shakes))


def _dudeslidesteps(matrix, color=None):
    """Drop the shades into place, then run the glint along the top bar."""
    face = DUDE
    for dy in (-3, -2, -1, 0):
        drawface(matrix, shift(face, dy=dy), color)
        yield 0.1
    yield 0.25
    for glint in (".WBBBB.", ".BWBBB.", ".BBWBB.", ".BBBWB.", ".BBBBW."):
        drawface(matrix, _replace(face, 1, glint), color)
        yield 0.09


def _dudeslide(matrix, color=None):
    return _drain(_dudeslidesteps(matrix, color))


def _pulsefacesteps(matrix, name, color=None, cycles=2):
    """Fade a face in and out -- the fallback animation."""
    _holdbrightness(matrix)
    drawface(matrix, getface(name), color)
    try:
        for _ in range(cycles):
            for level in (0.2, 0.4, 0.7, 1.0, 0.7, 0.4):
                matrix.set_brightness(_basebrightness() * level)
                matrix.show()
                yield 0.08
    finally:
        _releasebrightness(matrix)


def _pulseface(matrix, name, color=None, cycles=2):
    return _drain(_pulsefacesteps(matrix, name, color, cycles))


# ---------------------------------------------------------------- the map
FRAMES = {
    "happy": HAPPY_FRAMES,
    "neutral": NEUTRAL_FRAMES,
    "shy": SHY_FRAMES,
    "cry": CRY_FRAMES,
    "surprise": SURPRISE_FRAMES,
    "think": THINK_FRAMES,
    "sleep": SLEEP_FRAMES,
    "nerd": NERD_FRAMES,
}

SPECIALS = {
    "angry": _angryshake,
    "dude": _dudeslide,
    "heart": _heartbeat,
    "disco": _discosparkle,
}

#: The same four as step generators, for playing in the background. Kept as a
#: second map rather than replacing the one above, because SPECIALS is part of
#: what mieo re-exports and calling one of those should still just run it.
SPECIAL_STEPS = {
    "angry": _angryshakesteps,
    "dude": _dudeslidesteps,
    "heart": _heartbeatsteps,
    "disco": _discosparklesteps,
}


def _animatefacesteps(matrix, name, color=None, repeat=3, delay=FRAME_DELAY):
    """The animation that belongs to `name`, a frame at a time."""
    getface(name)  # fails early with a friendly message
    name = resolveemotion(name)
    if name in SPECIAL_STEPS:
        yield from SPECIAL_STEPS[name](matrix, color)
    elif name in FRAMES:
        yield from _playframessteps(matrix, FRAMES[name], color, repeat, delay)
    else:
        yield from _pulsefacesteps(matrix, name, color)


def _animateface(matrix, name, color=None, repeat=3, delay=FRAME_DELAY):
    """Play the animation that belongs to `name`."""
    return _drain(_animatefacesteps(matrix, name, color, repeat, delay))


def blink(matrix, name, color=None, times=1, shut=0.12, open_for=0.6):
    """Blink whichever face is showing -- handy on its own."""
    face = getface(name)
    lid = blink_frame(_lid_color(face))
    for _ in range(times):
        drawface(matrix, face, color)
        time.sleep(open_for)
        drawface(matrix, lid, color)
        time.sleep(shut)
    drawface(matrix, face, color)


def _lid_color(face):
    """Pick a lid colour: whatever the eyes are mostly made of."""
    counts = {}
    for row in face:
        for ch in row:
            if ch != ".":
                counts[ch] = counts.get(ch, 0) + 1
    if not counts:
        return "W"
    best = "W"
    for ch in counts:
        if counts[ch] > counts.get(best, 0):
            best = ch
    return best


# ---------------------------------------------------------- the patterns
# Light shows rather than faces: nothing here draws a bitmap, they just
# paint the panel for a few seconds and hand it back blank.

PATTERN_SECONDS = 3.0


def _scale(color, factor):
    """Dim a colour. factor 1.0 leaves it alone, 0.0 turns it off."""
    r, g, b = color
    return (int(r * factor), int(g * factor), int(b * factor))


def _rainbowsteps(matrix, seconds=PATTERN_SECONDS, delay=0.05):
    """A rainbow sliding sideways: one hue step per column, drifting."""
    step = 256 // matrix.width
    for t in range(int(seconds / delay)):
        for x in range(matrix.width):
            color = wheel(x * step + t * 6)
            for y in range(matrix.height):
                matrix.set_pixel(x, y, color)
        matrix.show()
        yield delay


def rainbow(matrix, seconds=PATTERN_SECONDS, delay=0.05):
    return _drain(_rainbowsteps(matrix, seconds, delay))


def _partysteps(matrix, seconds=PATTERN_SECONDS, delay=0.12):
    """The whole panel flashing a new colour on every beat."""
    for i in range(int(seconds / delay)):
        matrix.fill(DISCO_COLORS[i % len(DISCO_COLORS)])
        matrix.show()
        yield delay


def party(matrix, seconds=PATTERN_SECONDS, delay=0.12):
    return _drain(_partysteps(matrix, seconds, delay))


def _wavesteps(matrix, seconds=PATTERN_SECONDS, delay=0.06):
    """A bright band rolling down the panel, trailing a dimmer tail."""
    span = matrix.height + 2
    for t in range(int(seconds / delay)):
        matrix.clear()
        head = t % span
        color = wheel(t * 8)
        for y in range(matrix.height):
            behind = head - y
            if 0 <= behind <= 2:
                lit = _scale(color, (3 - behind) / 3.0)
                for x in range(matrix.width):
                    matrix.set_pixel(x, y, lit)
        matrix.show()
        yield delay


def wave(matrix, seconds=PATTERN_SECONDS, delay=0.06):
    return _drain(_wavesteps(matrix, seconds, delay))


def _chasesteps(matrix, seconds=PATTERN_SECONDS, delay=0.05):
    """One dot running through the LEDs in the order they are wired.

    Handy on its own: if this does not travel in neat rows, the wiring
    options on LedMatrix do not match how the panel is really built.
    """
    step = 256 // matrix.n
    for t in range(int(seconds / delay)):
        i = t % matrix.n
        matrix.clear()
        matrix.set_led(i, wheel(i * step))
        matrix.show()
        yield delay


def chase(matrix, seconds=PATTERN_SECONDS, delay=0.05):
    return _drain(_chasesteps(matrix, seconds, delay))


def _breathesteps(matrix, seconds=PATTERN_SECONDS, delay=0.05):
    """The panel swelling and fading, drifting through the colour wheel."""
    _holdbrightness(matrix)
    try:
        for t in range(int(seconds / delay)):
            # a triangle wave, 0.15 up to 1.0 and back down again
            phase = (t % 20) / 10.0
            level = 0.15 + 0.85 * (phase if phase <= 1 else 2 - phase)
            matrix.fill(wheel(t * 4))
            matrix.set_brightness(_basebrightness() * level)
            matrix.show()
            yield delay
    finally:
        _releasebrightness(matrix, show=False)


def breathe(matrix, seconds=PATTERN_SECONDS, delay=0.05):
    return _drain(_breathesteps(matrix, seconds, delay))


PATTERNS = {
    "rainbow": rainbow,
    "disco": _discosparkle,
    "party": party,
    "wave": wave,
    "chase": chase,
    "breathe": breathe,
}

#: The same six as step generators, for playing in the background.
PATTERN_STEPS = {
    "rainbow": _rainbowsteps,
    "disco": _discosparklesteps,
    "party": _partysteps,
    "wave": _wavesteps,
    "chase": _chasesteps,
    "breathe": _breathesteps,
}

# menu order (the block's dropdown follows this)
PATTERN_NAMES = ["rainbow", "disco", "party", "wave", "chase", "breathe"]


def _showpatternsteps(matrix, name):
    """One pattern by name, a frame at a time, blank at the end.

    The blanking is in a finally so that a pattern cut short by the next block
    does not leave half a frame lit, which is what the blocking version did by
    always reaching the end.
    """
    key = str(name).strip().lower()
    try:
        pattern = PATTERN_STEPS[key]
    except KeyError:
        raise ValueError(
            "unknown pattern '{}'. try one of: {}".format(
                name, ", ".join(PATTERN_NAMES)
            )
        )
    try:
        yield from pattern(matrix)
    finally:
        matrix.clear()
        matrix.show()


def _showpattern(matrix, name):
    """Play one pattern by name, then leave the panel blank."""
    return _drain(_showpatternsteps(matrix, name))


# ------------------------------------------------ playing in the background

#: Timer 1. Timer 0 belongs to sound.py, and Timer(-1) decodes to Timer(3) on
#: this port, which the Bluetooth REPL owns -- taking either would break
#: something that has nothing to do with the panel.
_ANIM_TIMER_ID = 1

#: How often the pump looks to see whether the next frame is due. Finer than
#: the fastest animation step (0.05s) so frames land close to their time,
#: coarse enough to cost nothing.
_ANIM_TICK_MS = 10

_anim_timer = None
_anim_steps = None
_anim_due = 0
_anim_pending = False


def _anim_tick(_timer_arg):
    """The timer's callback. Hands the real work to the scheduler.

    Nothing here touches the panel. Clocking pixels out to the strip takes a
    while with interrupts off, which is not something to do inside an
    interrupt. The pending flag stops the scheduler queue filling up if the
    board is busy with something else.
    """
    global _anim_pending
    # The timer is released when an animation ends, but one last tick can
    # already be queued behind the stop. Let it through for one global read.
    if _anim_steps is None or _anim_pending:
        return
    _anim_pending = True
    try:
        micropython.schedule(_anim_advance, 0)
    except Exception:
        # queue full. the next tick will try again
        _anim_pending = False


def _anim_advance(_arg):
    """Draw the next frame, if it is due. Runs from the scheduler.

    Deliberately incapable of raising: it runs in the gaps between whatever
    the program itself is doing, and an exception here would surface in the
    middle of somebody else's code.
    """
    global _anim_pending, _anim_due
    _anim_pending = False
    if _anim_steps is None:
        return
    try:
        now = time.ticks_ms()
        if time.ticks_diff(_anim_due, now) > 0:
            return
        delay = next(_anim_steps)
        wait = int((delay or 0) * 1000)
        # Count from when this frame was due, not from when it was drawn, so
        # that ten frames of a tenth of a second really do take a second.
        # Frames land on the timer's ticks, and measuring from the draw would
        # add each tick's lateness to the next. If the board fell more than a
        # frame behind, start again from now rather than rushing to catch up.
        due = time.ticks_add(_anim_due, wait)
        if time.ticks_diff(now, due) > 0:
            due = time.ticks_add(now, wait)
        _anim_due = due
    except StopIteration:
        stopsteps()
    except Exception:
        # A picture is not worth stopping the program for.
        stopsteps()


def startsteps(steps):
    """Play a step generator in the background and come straight back.

    The first frame is drawn here rather than on the next tick. That puts the
    animation up immediately, and it means a bad emotion or pattern name
    raises where the block was called instead of inside the timer, where
    nobody would ever see the message.
    """
    global _anim_timer, _anim_steps, _anim_due
    stopsteps()
    try:
        delay = next(steps)
    except StopIteration:
        return True
    except Exception:
        try:
            steps.close()
        except Exception:
            pass
        raise
    # The timer lives only while something is playing: stopsteps above let
    # the last one go. It is made before the animation is registered, so that
    # a timer that will not start leaves nothing behind that never advances;
    # closing the generator runs its finally, putting back whatever the first
    # frame changed.
    try:
        timer = machine.Timer(_ANIM_TIMER_ID)
        timer.init(period=_ANIM_TICK_MS, mode=machine.Timer.PERIODIC,
                   callback=_anim_tick)
    except Exception:
        try:
            steps.close()
        except Exception:
            pass
        raise
    _anim_timer = timer
    _anim_due = time.ticks_add(time.ticks_ms(), int((delay or 0) * 1000))
    _anim_steps = steps
    return True


def _releasetimer():
    """Let the timer go. It is cheap to make again and idle it costs nothing.

    Safe from anywhere this module calls it, including the end of an
    animation reached from the timer's own callback: on this port the
    callback and everything it schedules run in the main thread, not in the
    interrupt, so this is an ordinary deinit and not one from inside an ISR.
    """
    global _anim_timer
    timer = _anim_timer
    _anim_timer = None
    if timer is not None:
        try:
            timer.deinit()
        except Exception:
            pass


def stopsteps():
    """Stop whatever is playing in the background.

    Closing the generator runs the finally blocks inside it, so a stopped
    animation still puts the brightness back and a stopped pattern still
    blanks the panel. The timer goes too, so an idle board is not woken a
    hundred times a second for nothing.
    """
    global _anim_steps
    steps = _anim_steps
    _anim_steps = None
    _releasetimer()
    if steps is not None:
        try:
            steps.close()
        except Exception:
            pass
    return True


def isbusy():
    """True while something is still playing in the background."""
    return _anim_steps is not None


# ------------------------------------------- the font, and scrolling text
# ----------------------------------------------------------------- font
# rows are taken straight from font_5x7[] / font_lower_special[]
#: every character, in one blob: five bytes each, in FONT_ORDER order.
#: the rows are exactly the font_5x7[] tables from the Arduino sketch --
#: keeping them as one bytes object instead of 79 tuples of five numbers
#: is far less for MicroPython to compile and to hold in memory.
FONT_ORDER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefghijklmnopqrstuvwxyz !?.,:;'-_+=/\\$#@"
_FONT_BLOB = b'\x1c"">"<"<"<\x1c" "\x1c<"""<> < >> <  \x1e &"\x1c"">""\x1c\x08\x08\x08\x1c\x06\x02\x02"\x1c"$8$"    >"6*"""2*&"\x1c"""\x1c<"<  \x1c""&\x1e<"<$"\x1e \x1c\x02<>\x08\x08\x08\x08""""\x1c"""\x14\x08""*6""\x14\x08\x14""\x14\x08\x08\x08>\x04\x08\x10>\x1c"""\x1c\x08\x18\x08\x08\x1c\x1c"\x04\x08><\x02\x1c\x02<\x04\x0c\x14>\x04> <\x02<\x1e <"\x1c>\x02\x04\x08\x10\x1c"\x1c"\x1c\x1c"\x1e\x02<\x1c\x02\x1e"\x1e <""<\x00\x1c  \x1c\x02\x1e""\x1e\x1c"> \x1c\x0c\x10<\x10\x10\x1e"\x1e\x02< <"""\x08\x00\x08\x08\x08\x02\x02\x02"\x1c $8$"\x18\x08\x08\x08\x1c\x006*""\x00<"""\x00\x1c""\x1c<"<  \x1e"\x1e\x02\x02\x00,2  \x1e \x1c\x02<\x10<\x10\x10\x0e\x00"""\x1e\x00""\x14\x08\x00"*6"\x00\x14\x08\x14"""\x1e\x02\x1c\x00>\x08\x10>\x00\x00\x00\x00\x00\x08\x08\x08\x00\x08\x1c\x02\x0c\x00\x08\x00\x00\x00\x00\x08\x00\x00\x00\x08\x10\x00\x08\x00\x08\x00\x00\x08\x00\x08\x10\x08\x08\x00\x00\x00\x00\x00>\x00\x00\x00\x00\x00\x00>\x08\x08>\x08\x08\x00>\x00>\x00\x02\x04\x08\x10  \x10\x08\x04\x02\x1e(\x1c\n<\x14>\x14>\x14<R^@<'


class _Font:
    """Looks and behaves like the old dict, without building one."""

    def __len__(self):
        return len(FONT_ORDER)

    def __contains__(self, ch):
        return ch in FONT_ORDER

    def __getitem__(self, ch):
        i = FONT_ORDER.find(ch)
        if i < 0:
            raise KeyError(ch)
        i *= 5
        return _FONT_BLOB[i:i + 5]

    def get(self, ch, default=None):
        i = FONT_ORDER.find(ch)
        if i < 0:
            return default
        i *= 5
        return _FONT_BLOB[i:i + 5]

    def keys(self):
        return [c for c in FONT_ORDER]

    def items(self):
        return [(c, self[c]) for c in FONT_ORDER]


FONT = _Font()

GLYPH_HEIGHT = 5  # every glyph is 5 rows tall


# ------------------------------------------------------------- rendering
def _bit_length(value):
    """How many bits `value` needs (MicroPython friendly)."""
    n = 0
    while value:
        value >>= 1
        n += 1
    return n


def glyph_columns(ch):
    """One glyph as a list of columns, each column a list of 5 on/off rows."""
    rows = FONT.get(ch)
    if rows is None:
        rows = FONT.get(ch.upper(), FONT.get(ch.lower(), FONT["?"]))
    # the font leaves bit 0 empty, so a normal letter lives in bits 5..1.
    # a couple of the wide symbols (@) reach up to bit 6, hence the max().
    width = max(5, _bit_length(max(rows)) - 1)
    return [[(row >> (width - x)) & 1 for row in rows] for x in range(width)]


def wheel(pos):
    """0-255 around the colour wheel -> (r, g, b). Handy for rainbows."""
    pos = pos % 256
    if pos < 85:
        return (255 - pos * 3, pos * 3, 0)
    if pos < 170:
        pos -= 85
        return (0, 255 - pos * 3, pos * 3)
    pos -= 170
    return (pos * 3, 0, 255 - pos * 3)


def _color_picker(color):
    """Return a function(char_index, column_index) -> (r, g, b)."""
    if color == "rainbow":
        return lambda ci, x: wheel(x * 12)
    if isinstance(color, (list, tuple)) and color and isinstance(color[0], (list, tuple)):
        palette = list(color)
        return lambda ci, x: palette[ci % len(palette)]
    solid = color or YELLOW
    return lambda ci, x: solid


def text_columns(message, color=YELLOW, spacing=1):
    """The whole message as a list of (column, colour) pairs."""
    pick = _color_picker(color)
    columns = []
    for ci, ch in enumerate(message):
        for col in glyph_columns(ch):
            columns.append((col, pick(ci, len(columns))))
        for _ in range(spacing):
            columns.append(([0] * GLYPH_HEIGHT, OFF))
    return columns


def _blit(matrix, columns, offset, y_offset=0):
    """Draw the window of `columns` starting at `offset` onto the panel."""
    matrix.clear()
    rows = min(GLYPH_HEIGHT, matrix.height - y_offset)
    for x in range(matrix.width):
        i = offset + x
        if not (0 <= i < len(columns)):
            continue
        col, col_color = columns[i]
        for y in range(rows):
            if col[y]:
                matrix.set_pixel(x, y + y_offset, col_color)
    matrix.show()


def scrolltext(matrix, message, color=YELLOW, delay=0.20, spacing=1, repeat=1,
           y_offset=0):
    """Slide a message across the panel, right to left."""
    columns = text_columns(str(message), color, spacing)
    blank = ([0] * GLYPH_HEIGHT, OFF)
    strip = [blank] * matrix.width + columns + [blank] * matrix.width
    for _ in range(repeat):
        for offset in range(len(strip) - matrix.width + 1):
            _blit(matrix, strip, offset, y_offset)
            time.sleep(delay)
    matrix.clear()
    matrix.show()


def showchar(matrix, ch, color=YELLOW, seconds=1.0, y_offset=0):
    """Put one character on the panel and leave it there (centred)."""
    columns = [(col, _color_picker(color)(0, x))
               for x, col in enumerate(glyph_columns(str(ch)[0]))]
    pad = max(0, (matrix.width - len(columns)) // 2)
    _blit(matrix, [([0] * GLYPH_HEIGHT, OFF)] * pad + columns, 0, y_offset)
    if seconds:
        time.sleep(seconds)


def spelltext(matrix, message, color=YELLOW, seconds=0.4, gap=0.1):
    """Show a message one letter at a time instead of scrolling."""
    for ch in str(message):
        showchar(matrix, ch, color, seconds)
        if gap:
            matrix.clear()
            matrix.show()
            time.sleep(gap)
