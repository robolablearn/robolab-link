# boot.py -- written by Robolab, runs before main.py on every power on.
#
# Three jobs, in this order and for this reason:
#
# First, put the filesystem right. If a Bluetooth upload was cut off part way
# through -- the board carried out of range, a flat battery, the editor closed
# -- there is a half-written file staged on flash. It has to be dealt with
# before main.py runs, so the program that runs is always a whole one.
#
# Then bring Bluetooth up, so the board can be found and programmed without the
# cable. It needs nothing set up and no network: the board simply advertises
# itself, and Robolab lists it in the connect window alongside the COM ports.
#
# The radio goes up before anything else is imported, and that ordering is the
# whole reason this file is arranged the way it is. The Bluetooth controller
# wants a large contiguous block of internal memory, and it is the one thing
# here that cannot be made to wait and ask again later. Importing mieo first --
# which drags in the display, motor, sensor and sound modules and their tables
# -- leaves the heap too broken up to satisfy it, and the controller then fails
# to start with "hci inits failed". That is not an exception the try below can
# catch: the stack faults on the host it never managed to build, and the board
# reboots into the same failure over and over, invisible to Bluetooth and
# unreachable over the cable alike. So the radio comes first.
#
# Last, the lamp. Lighting it means importing mieo, which is exactly the heavy
# import that has to stay behind the radio -- and only a board with no program
# of its own needs it, to show it is waiting to be given one.
try:
    import mieoupload

    _state = mieoupload.recover()
    if _state != "clean":
        print("Mieo upload recovery:", _state)
    del _state
except Exception as _e:
    # Recovery failing must not stop the board booting. The worst case is a
    # stale staging file taking up room, which the next upload clears anyway.
    print("Mieo upload recovery unavailable:", _e)
    del _e

try:
    import mieoble

    print("Mieo on Bluetooth as", mieoble.start())
except Exception as _e:
    # Boot must never leave the board dead. Whatever went wrong with the
    # radio, main.py still gets to run and the USB cable still works.
    print("Mieo Bluetooth unavailable:", _e)
    del _e

try:
    import os

    if "main.py" not in os.listdir():
        import mieo

        mieo.bluetoothindicator(True)
except Exception as _e:
    print("Mieo startup indicator unavailable:", _e)
    del _e
