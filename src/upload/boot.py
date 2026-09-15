# boot.py -- written by Robolab, runs before main.py on every power on.
#
# Two jobs, in this order and for this reason:
#
# First, put the filesystem right. If a Bluetooth upload was cut off part way
# through -- the board carried out of range, a flat battery, the editor closed
# -- there is a half-written file staged on flash. It has to be dealt with
# before main.py runs, so the program that runs is always a whole one.
#
# Then bring Bluetooth up, so the board can be found and programmed without the
# cable. It needs nothing set up and no network: the board simply advertises
# itself, and Robolab lists it in the connect window alongside the COM ports.
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
