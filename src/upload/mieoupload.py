"""Receiving a file on the board, safely.

Robolab sends programs over Bluetooth by running statements in the board's
REPL. That is a reliable pipe -- Bluetooth checksums and re-sends every packet
underneath, and every statement is answered before the next one is sent -- but
a pipe is not the whole story. The board can still lose power, run out of
flash, or have the radio walk out of range half way through, and none of those
may be allowed to damage the program that is already working.

So nothing is ever written over the live program. Bytes land in "<name>.tmp",
the finished file is read back off flash and checked against the CRC the editor
worked out before it sent anything, and only then does the new file take the
old one's place in a single rename. Lose power at any point and the board still
has a program: either the old one or the new one, never half of one.

The sequence numbers and the CRC here are not standing in for the transport --
they catch the board's own failures. A statement that never made it out of the
buffer, a flash write that silently did nothing, a session resumed after a
reconnect that is not the session the editor thinks it is.

This runs from the REPL, on the main task. It is deliberately NOT part of the
Bluetooth callback: an uncaught exception in that callback disables the radio
until somebody power cycles the board, and a file parser has far too many ways
to raise.
"""

import binascii
import os

#: Bumped if the wire format ever changes. The editor reads it back from begin().
VERSION = 1

#: The only names that may be written. Everything the editor legitimately sends
#: is here and nothing else is: a name is not a path, so a typo or a mangled
#: statement cannot land somewhere unexpected.
#: Must stay in step with LIBRARY_FILES in mieo-library.js -- a library file
#: missing from here is refused as BADNAME and simply never reaches the board.
ALLOWED = (
    "main.py",
    "libver.json",
    "display.py",
    "sound.py",
    "pins.py",
    "motors.py",
    "linefollower.py",
    "mieo.py",
    "mieoble.py",
    "mieoupload.py",
    "boot.py",
)

#: Verification reads back in steps this size, so a large file never has to be
#: held in memory all at once.
READ_STEP = 512

#: The largest chunk a single statement may carry. Matches what the editor
#: sends; anything larger would be a mangled statement rather than a big one.
MAX_CHUNK = 512

_f = None
_name = ""
_expected = 0
_crc = 0
_want = 0
_size = 0
_chunk = 0
_written = 0
_session = 0

#: Set once the sidecar is on flash, which is the point of no return: from then
#: on the staged file is the good copy and must survive even if the swap itself
#: fails, because the live file may already have been removed to make room for
#: it. Only recover() may finish or undo the swap after this.
_approved = False


def _say(message):
    """Answer the editor.

    Printed rather than returned: the REPL hands stdout back to whoever ran the
    statement, and a returned value would simply be discarded. The value comes
    back as well so the same functions can be driven from a test.
    """
    print(message)
    return message


def _sync():
    # Not present under CPython, which is where the tests for this file run.
    if hasattr(os, "sync"):
        os.sync()


def _exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _unlink(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _crc_file(path):
    """CRC-32 of a file, read back off flash rather than remembered.

    Reading it back is the whole point: it is what proves the bytes reached the
    filesystem, not merely a buffer on the way there.
    """
    crc = 0
    f = open(path, "rb")
    try:
        while True:
            block = f.read(READ_STEP)
            if not block:
                break
            crc = binascii.crc32(block, crc) & 0xFFFFFFFF
    finally:
        f.close()
    return crc


def _free_bytes():
    """Room left on the filesystem, or None if the board will not say."""
    try:
        stat = os.statvfs("/")
        return stat[0] * stat[3]
    except (OSError, AttributeError):
        return None


def _replace(tmp, name):
    """Put the staged file in place of the live one.

    On this board's filesystem rename replaces the destination in a single
    commit, which is what makes the swap atomic, so it is tried that way first.
    Some filesystems refuse to rename onto a name that already exists; there the
    old file has to go first. The ".sum" sidecar written before this point is
    what makes that second path safe -- lose power in the gap and recover()
    finds the sidecar beside the staged file on the next boot and finishes the
    swap, so the board is never left without a program.
    """
    try:
        os.rename(tmp, name)
    except OSError as e:
        # Only "the destination is already in the way" justifies removing a
        # working program. Any other failure -- no space left, bad flash --
        # would be hit again by the retry, so unlinking first would destroy
        # the old program to gain nothing.
        if len(e.args) and e.args[0] != 17:      # 17 is EEXIST
            raise
        _unlink(name)
        os.rename(tmp, name)


def _reset():
    global _f, _name, _expected, _crc, _want, _size, _chunk, _written, _session
    global _approved
    if _f is not None:
        try:
            _f.close()
        except OSError:
            pass
    _f = None
    _name = ""
    _expected = 0
    _crc = 0
    _want = 0
    _size = 0
    _chunk = 0
    _written = 0
    _session = 0
    _approved = False


def begin(name, size, crc, chunk):
    """Start receiving `name`. Answers "OK <version> <session>".

    A begin while another transfer is in flight is a restart, not an error --
    that is what a reconnect looks like from here.
    """
    global _f, _name, _expected, _crc, _want, _size, _chunk, _written, _session

    _reset()

    if name not in ALLOWED or "/" in name or "\\" in name or ".." in name:
        return _say("ERR BADNAME")
    if chunk < 1 or chunk > MAX_CHUNK or size < 0:
        return _say("ERR BADARG")

    free = _free_bytes()
    # Room for the file itself plus its sidecar and the metadata the rename
    # needs: filling the filesystem to exactly zero is how a commit fails at
    # the very last step, which is the most expensive place to fail.
    if free is not None and free < (size + 2048):
        return _say("ERR NOSPACE {} {}".format(size, free))

    tmp = name + ".tmp"
    # Anything left over from a previous attempt is not worth trusting.
    _unlink(tmp)
    _unlink(name + ".sum")

    try:
        _f = open(tmp, "wb")
    except OSError as e:
        _f = None
        return _say("ERR {}".format(e))

    _name = name
    _size = size
    _want = crc & 0xFFFFFFFF
    _chunk = chunk
    _expected = 0
    _crc = 0
    _written = 0
    # Enough to tell one transfer from another after a reconnect. Derived from
    # the file rather than drawn at random, because there is no clock here
    # worth using and the editor knows all three inputs already.
    _session = (crc ^ (size << 3) ^ len(name)) & 0x7FFFFFFF
    return _say("OK {} {}".format(VERSION, _session))


def w(seq, data):
    """Take one chunk. Answers A accepted, D duplicate, G gap, L bad length.

    Never raises. A transfer that has gone wrong has to be able to say so in
    one short line: a traceback arriving where the editor expects a reply would
    strand it waiting for one that has already gone past.
    """
    global _f, _expected, _crc, _written

    try:
        if _f is None:
            return _say("E no transfer in progress")

        if seq < _expected:
            # Already on flash. Say where to carry on from, and write nothing.
            return _say("D {}".format(_expected))
        if seq > _expected:
            return _say("G {}".format(_expected))

        raw = binascii.a2b_base64(data)

        # Only the last chunk may be short. Anything else means the statement
        # arrived mangled, and writing it would quietly corrupt the file.
        if _written + len(raw) > _size:
            return _say("L {}".format(_expected))
        if len(raw) != _chunk and (_written + len(raw)) != _size:
            return _say("L {}".format(_expected))

        _f.write(raw)
        _crc = binascii.crc32(raw, _crc) & 0xFFFFFFFF
        _written += len(raw)
        _expected += 1
        return _say("A {}".format(_expected))
    except Exception as e:
        try:
            if _f is not None:
                _f.close()
        except OSError:
            pass
        _f = None
        _unlink(_name + ".tmp")
        return _say("E {}".format(e))


def commit():
    """Check what actually landed on flash, then put it in place.

    Answers "OK", or says what was wrong and leaves the old file alone.
    """
    global _f, _approved

    try:
        if _f is None:
            return _say("ERR no transfer in progress")

        name = _name
        tmp = name + ".tmp"
        _f.close()
        _f = None
        _sync()

        if _written != _size:
            _unlink(tmp)
            _reset()
            return _say("ERR SHORT {} {}".format(_size, _written))

        actual = _crc_file(tmp)
        if actual != _want:
            # The live file has not been touched, so the board still works.
            _unlink(tmp)
            _reset()
            return _say("CRC {}".format(actual))

        # The sidecar marks the swap as begun and approved. If power is lost
        # between here and the rename, recover() finds it and finishes the job.
        s = open(name + ".sum", "w")
        try:
            s.write("{}\n".format(_want))
        finally:
            s.close()
        _sync()
        _approved = True

        # Same directory, so on littlefs this is a single metadata commit and
        # the name refers to either the old file or the new one at every
        # instant. Never remove the target first on that path: rename replaces
        # it, and removing would manufacture the very gap this is avoiding.
        _replace(tmp, name)
        _sync()
        _unlink(name + ".sum")
        _sync()

        try:
            import mieo
            mieo.bluetoothindicator(False)
        except Exception:
            pass

        _reset()
        return _say("OK")
    except Exception as e:
        failed = _name
        approved = _approved
        _reset()
        # Only throw the staged file away if the swap was NOT approved. Once the
        # sidecar is on flash the staged copy may be the only whole copy left --
        # the live file can already have been removed to make room for it -- so
        # deleting it here is exactly how a board ends up with no program at
        # all. Left alone, recover() finishes the job on the next boot.
        if failed and not approved:
            _unlink(failed + ".tmp")
        return _say("ERR {}".format(e))


def status():
    """Where a transfer got to, so a reconnect can carry on from there."""
    if _f is None:
        return _say("NONE")
    return _say("{} {} {} {} {}".format(_session, _name, _expected, _written, _crc))


def abort():
    """Throw a transfer in progress away. Safe to call at any time."""
    name = _name
    _reset()
    if name:
        _unlink(name + ".tmp")
        _unlink(name + ".sum")
    return _say("OK")


def recover():
    """Put the filesystem back in order after a transfer that did not finish.

    Called from boot.py before anything else runs, so that main.py is known to
    be whole before it is executed.

    The rename in commit() is already atomic on this board's filesystem, so
    strictly the stale ".tmp" case is the only one that can happen here. The
    rest costs a few lines and keeps the guarantee if the board is ever
    reflashed onto a filesystem where replacing a file is not one step.
    """
    # At boot nothing is ever in flight, but if this is ever called while a
    # transfer is open, that handle has to go before its file can be tidied
    # away -- on some filesystems an open file cannot be removed at all, and
    # the failure would be silent.
    if _f is not None:
        _reset()

    fixed = []
    try:
        names = os.listdir()
    except OSError:
        return "unreadable"

    for entry in names:
        if not entry.endswith(".tmp"):
            continue
        name = entry[:-4]
        if name not in ALLOWED:
            continue
        sidecar = name + ".sum"

        if not _exists(sidecar):
            # Never got as far as being verified, so the old file is still the
            # good one. This is what a disconnect mid-upload leaves behind.
            #
            # Unless there is no old file. Then this unverified fragment is the
            # only copy of anything, and deleting it would throw away the only
            # thing left; leave it for the next upload to clear.
            if not _exists(name):
                fixed.append("kept " + entry + " (no " + name + ")")
                continue
            _unlink(entry)
            fixed.append("dropped " + entry)
            continue

        try:
            s = open(sidecar)
            try:
                want = int(s.read().strip())
            finally:
                s.close()
        except (OSError, ValueError):
            _unlink(entry)
            _unlink(sidecar)
            fixed.append("dropped " + entry)
            continue

        try:
            matches = _crc_file(entry) == want
        except OSError:
            matches = False

        if matches:
            # The swap had been approved and was interrupted. Finish it.
            _replace(entry, name)
            _sync()
            _unlink(sidecar)
            fixed.append("completed " + name)
        elif _exists(name):
            _unlink(entry)
            _unlink(sidecar)
            fixed.append("dropped " + entry)
        else:
            # Damaged, but still the only copy. Keep it rather than leave the
            # board with nothing at all; the next upload replaces it.
            _unlink(sidecar)
            fixed.append("kept damaged " + entry + " (no " + name + ")")

    # A sidecar with no .tmp beside it means the rename landed and only the
    # tidying up was lost.
    for entry in names:
        if not entry.endswith(".sum"):
            continue
        # Only ever this module's own sidecars: ".sum" is a common enough
        # suffix that sweeping every match would delete somebody's data file.
        if entry[:-4] not in ALLOWED:
            continue
        if not _exists(entry[:-4] + ".tmp"):
            _unlink(entry)

    return ", ".join(fixed) if fixed else "clean"
