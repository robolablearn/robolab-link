"""Bluetooth for Mieo: the REPL, carried over BLE.

The board advertises itself as "Mieo-XXXX" and offers a Nordic UART service --
the usual pair of characteristics that stand in for a serial cable. Robolab
connects to it, and MicroPython's own REPL is put on the other end with
os.dupterm, so everything that works down the USB cable works here: running
statements live, and writing files to send a program.

Bluetooth on the ESP32 is BLE only. There is no Bluetooth Classic serial port
here, so the board turns up in Robolab's connection window as a Bluetooth
device rather than as a COM port -- and firmware still has to go down the
cable, because the chip's bootloader has no radio at all.

Started by boot.py, so the board is reachable from the moment it powers on.
"""

import binascii
import io
import json
import os

import bluetooth
import machine
import micropython
from micropython import const

#: Where the board's name is kept, if it has been given one.
CONFIG_FILE = "ble.json"

_IRQ_CENTRAL_CONNECT = const(1)
_IRQ_CENTRAL_DISCONNECT = const(2)
_IRQ_GATTS_WRITE = const(3)
_IRQ_MTU_EXCHANGED = const(21)

_FLAG_WRITE = const(0x0008)
_FLAG_NOTIFY = const(0x0010)
_FLAG_WRITE_NO_RESPONSE = const(0x0004)

_MP_STREAM_POLL = const(3)
_MP_STREAM_POLL_RD = const(0x0001)

#: The Nordic UART service. Not an official standard, but it is what every
#: "BLE serial" tool speaks, so anything else can talk to the board too.
_UART_UUID = bluetooth.UUID("6E400001-B5A3-F393-E0A9-E50E24DCCA9E")
_UART_TX = (
    bluetooth.UUID("6E400003-B5A3-F393-E0A9-E50E24DCCA9E"),
    _FLAG_NOTIFY,
)
_UART_RX = (
    bluetooth.UUID("6E400002-B5A3-F393-E0A9-E50E24DCCA9E"),
    _FLAG_WRITE | _FLAG_WRITE_NO_RESPONSE,
)
_UART_SERVICE = (_UART_UUID, (_UART_TX, _UART_RX))

#: Ctrl-C. Watched for by hand rather than left to the terminal layer: nothing
#: reads this stream while a program is looping, so a stop has to be noticed
#: the moment it arrives, in the Bluetooth callback itself.
_INTERRUPT = const(3)

#: How much the board will hold for a central that is not keeping up.
_RX_BUFFER = const(512)

#: Gap between flushes of pending output. Long enough to gather a line or two
#: into one notification, short enough that typing feels live.
_FLUSH_MS = const(40)

_uart = None
_stream = None
_name = None


def name():
    """What this board calls itself when advertising.

    Named after its own MAC address, so a room full of boards stays tellable
    apart with nothing to configure.
    """
    global _name
    if _name is None:
        saved = ""
        try:
            with open(CONFIG_FILE) as f:
                saved = json.load(f).get("name", "")
        except (OSError, ValueError):
            saved = ""
        if saved:
            _name = saved
        else:
            tail = binascii.hexlify(machine.unique_id()[-2:]).decode().upper()
            _name = "Mieo-{}".format(tail)
    return _name


def setname(value):
    """Rename this board. Takes effect at the next power on."""
    global _name
    with open(CONFIG_FILE, "w") as f:
        json.dump({"name": value}, f)
    _name = value


def _advertising_payload(name_):
    """The bytes the board broadcasts.

    Flags and a name only. The UART service UUID is 128 bits, and it plus a
    name does not fit in the 31 bytes an advertisement gets -- so Robolab finds
    boards by name and asks for the service after connecting.
    """
    payload = bytearray()

    def append(adv_type, value):
        payload.extend(bytes((len(value) + 1, adv_type)))
        payload.extend(value)

    append(0x01, bytes((0x06,)))            # general discoverable, no BR/EDR
    append(0x09, name_.encode())            # complete local name
    return payload


class BLEUART:
    """The two characteristics, and the buffers behind them."""

    def __init__(self, ble, name_):
        self._ble = ble
        self._ble.active(True)
        # Ask for a bigger MTU than the 23-byte default, which is what decides
        # how many bytes fit in one packet and so how long an upload takes.
        # After active(True), not before: the stack refuses the setting while
        # it is off, and does so quietly. The central has the final say, so
        # nothing here assumes it was granted -- see _IRQ_MTU_EXCHANGED.
        try:
            self._ble.config(mtu=247)
        except Exception:
            # Older firmware without the option. 23 bytes still works.
            pass
        self._ble.config(gap_name=name_)
        self._ble.irq(self._irq)
        ((self._tx_handle, self._rx_handle),) = self._ble.gatts_register_services(
            (_UART_SERVICE,)
        )
        # Append mode: a central sending faster than the board reads must have
        # its writes queued rather than each one replacing the last.
        self._ble.gatts_set_buffer(self._rx_handle, _RX_BUFFER, True)
        self._connections = set()
        self._rx_buffer = bytearray()
        self._handler = None
        self._mtu = 23
        self._payload = _advertising_payload(name_)
        self._advertise()

    def irq(self, handler):
        self._handler = handler

    def _advertise(self, interval_us=500000):
        self._ble.gap_advertise(interval_us, adv_data=self._payload)

    def _irq(self, event, data):
        """Every Bluetooth event arrives here.

        The wrapper is the whole point. An exception that escapes this callback
        does not merely print a traceback: the stack drops the handler, and the
        radio then goes deaf until somebody power cycles the board, with
        nothing on screen to say why. A dropped event is recoverable. A dropped
        handler is not.
        """
        try:
            self._irq_body(event, data)
        except Exception as e:
            print("Mieo Bluetooth event failed:", e)

    def _irq_body(self, event, data):
        if event == _IRQ_CENTRAL_CONNECT:
            conn_handle, _, _ = data
            self._connections.add(conn_handle)
            # The REPL is put on the radio only now, when there is finally
            # something on the other end of it. Leaving it attached the rest of
            # the time is what made the board crash during an ordinary USB
            # upload: a soft reset over the cable tears the interpreter down
            # while this terminal is still wired into it.
            _attach()
            # Nothing starts the MTU exchange on its own here, and without it
            # the link stays at 23 bytes however large a packet either side
            # would have accepted.
            try:
                self._ble.gattc_exchange_mtu(conn_handle)
            except Exception:
                pass
            _updateindicator()
        elif event == _IRQ_CENTRAL_DISCONNECT:
            conn_handle, _, _ = data
            self._connections.discard(conn_handle)
            self._mtu = 23
            if not self._connections:
                # Nobody left to talk to, so take the REPL back off the radio.
                _detach()
            # Straight back to advertising, so closing the editor and opening
            # it again finds the board without a power cycle.
            self._advertise()
            _updateindicator()
        elif event == _IRQ_MTU_EXCHANGED:
            _, mtu = data
            self._mtu = mtu
        elif event == _IRQ_GATTS_WRITE:
            conn_handle, value_handle = data
            if value_handle == self._rx_handle:
                chunk = self._ble.gatts_read(self._rx_handle)
                if _INTERRUPT in chunk:
                    # Stop whatever is running. Scheduled rather than raised
                    # here, because this is a callback from the Bluetooth
                    # stack: the exception has to surface in the program.
                    try:
                        micropython.schedule(_interrupt, 0)
                    except RuntimeError:
                        pass
                self._rx_buffer += chunk
                if self._handler:
                    self._handler()

    def any(self):
        return len(self._rx_buffer)

    def read(self, size=None):
        if not self._rx_buffer:
            return None
        if size is None:
            size = len(self._rx_buffer)
        result = self._rx_buffer[0:size]
        self._rx_buffer = self._rx_buffer[size:]
        return bytes(result)

    def write(self, data):
        """Send bytes to whoever is connected, a notification at a time.

        A notification cannot carry more than the negotiated MTU less three
        bytes of header, so longer output is split. There is nothing to do
        about a central that has gone away mid-write except carry on.
        """
        limit = self._mtu - 3
        for conn_handle in self._connections:
            for start in range(0, len(data), limit):
                try:
                    self._ble.gatts_notify(
                        conn_handle, self._tx_handle, data[start:start + limit]
                    )
                except OSError:
                    break

    def mtu(self):
        return self._mtu

    def connected(self):
        return len(self._connections) > 0


def _indicator(_arg):
    """Bring the Bluetooth lamp into line with the link.

    mieo is imported here rather than at the top because mieo imports this
    module in turn. Wrapped because a panel that will not light is no reason
    to lose a Bluetooth event.
    """
    try:
        import mieo
        mieo.bluetoothindicator(False)
    except Exception:
        pass


def _updateindicator():
    """Ask for the lamp once the radio callback is over.

    Lighting a pixel clocks the whole panel out with interrupts disabled,
    which is not something to do inside a Bluetooth event. Scheduling it runs
    the same work a moment later with the radio no longer waiting on us. A
    full queue is not worth an exception -- the next event refreshes it.
    """
    try:
        micropython.schedule(_indicator, 0)
    except Exception:
        pass


def _interrupt(_arg):
    raise KeyboardInterrupt


class BLEUARTStream(io.IOBase):
    """Makes the BLE link look like a terminal, so os.dupterm will take it.

    Output is gathered and flushed on a timer rather than sent as it is
    produced: a print statement can easily produce more bytes than one
    notification carries, and sending them one call at a time is far slower
    than sending them in batches.
    """

    def __init__(self, uart):
        self._uart = uart
        self._tx_buf = bytearray()
        # Timer 3, by number. Timer(-1) used to decode to Timer(3) on this port,
        # but MicroPython 1.25 rejects it as "invalid Timer number" -- and the
        # radio is already up by then, so the board still connects over
        # Bluetooth with no REPL behind it. Sound, display and the line
        # follower hold timers 0 to 2.
        self._timer = machine.Timer(3)
        self._uart.irq(self._on_rx)

    def _on_rx(self):
        # Wakes the terminal layer so it comes and reads what just arrived.
        if hasattr(os, "dupterm_notify"):
            os.dupterm_notify(None)

    def read(self, size=None):
        return self._uart.read(size)

    def readinto(self, buf):
        available = self._uart.read(len(buf))
        if available is None:
            return None
        for i in range(len(available)):
            buf[i] = available[i]
        return len(available)

    def ioctl(self, op, arg):
        if op == _MP_STREAM_POLL:
            if self._uart.any():
                return _MP_STREAM_POLL_RD
        return 0

    def _later(self, handler):
        self._timer.init(
            mode=machine.Timer.ONE_SHOT,
            period=_FLUSH_MS,
            callback=lambda _t: handler(),
        )

    def _flush(self):
        # As much as one notification can carry, but never less than the 100
        # bytes this used to send unconditionally. Going below that was a real
        # mistake: it does not make anything safer, and it multiplies how often
        # the timer below has to be re-armed to move the same output.
        limit = max(100, self._uart.mtu() - 3)
        chunk = self._tx_buf[0:limit]
        self._tx_buf = self._tx_buf[limit:]
        if chunk:
            self._uart.write(chunk)
        if self._tx_buf:
            self._later(self._flush)

    def drop(self):
        """Throw away anything still queued. Called when the central leaves."""
        self._tx_buf = bytearray()

    def write(self, buf):
        # With nobody connected there is no one to send to, so buffering the
        # output and starting a timer only to discover that is pure waste --
        # and it is not free: this runs on every byte the REPL prints over the
        # USB cable, and re-arming a one-shot timer from inside its own
        # callback is exactly the pattern that panics the ESP32.
        if not self._uart.connected():
            self._tx_buf = bytearray()
            return len(buf)

        was_empty = not self._tx_buf
        self._tx_buf += buf
        if was_empty:
            self._later(self._flush)
        return len(buf)


def mtu():
    """Bytes one packet can carry on this link, as actually negotiated.

    Robolab asks for this and sizes its writes to match. 23 is the Bluetooth
    default, and what a board that has not negotiated anything larger reports.
    """
    return _uart.mtu() if _uart is not None else 23


def connected():
    return _uart is not None and _uart.connected()


def _attach():
    """Put the REPL on the Bluetooth link."""
    if _stream is not None:
        os.dupterm(_stream)


def _detach():
    """Take the REPL back off it, leaving the USB cable as the only terminal."""
    try:
        os.dupterm(None)
    except Exception:
        pass
    if _stream is not None:
        _stream.drop()


def start():
    """Advertise, and be ready to carry the REPL. Returns the board's name.

    The terminal itself is not attached here. It goes on when a central
    connects and comes off again when it leaves, so a board that nobody has
    connected to behaves exactly like one without Bluetooth at all -- which is
    what keeps an ordinary USB upload working.
    """
    global _uart, _stream

    if _uart is None:
        _uart = BLEUART(bluetooth.BLE(), name())
        _stream = BLEUARTStream(_uart)
    return name()


def stop():
    """Put the REPL back on the cable alone and stop advertising."""
    global _uart, _stream

    os.dupterm(None)
    if _uart is not None:
        _uart._ble.active(False)
    _uart = None
    _stream = None
    # The radio is down, so nothing can be connected: put the lamp out. Done
    # here rather than through the scheduler because this is ordinary code,
    # not a Bluetooth callback.
    _indicator(0)
