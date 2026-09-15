const fs = require('fs');
const path = require('path');
const ansi = require('ansi-string');

const WebRepl = require('../lib/webrepl');
const {sendStop} = require('../lib/mieo-discovery');
const {LIBRARY_FILES, VERSION_FILE, checksum, readLibrary} = require('../lib/mieo-library');

/**
 * Sending a program to a Mieo over WiFi.
 *
 * CURRENTLY SWITCHED OFF. Nothing reaches this: the device no longer asks the
 * link server to look for boards on the network, and the board-side half is
 * not installed any more, because Bluetooth replaced it. The transport is kept
 * because it works and was tested -- deleting it would be the hard part to
 * undo. To turn it back on, put `wifi: true` back in the device's options,
 * restore mieowifi.py, and add it to LIBRARY_FILES.
 *
 * The same job esp32MicroPython.js does down the cable, except that mpremote
 * and esptool are not in the picture: everything goes through the board's
 * WebREPL. Firmware is the one thing that cannot come this way at all -- see
 * flashRealtimeFirmware below.
 */

/** How long to keep trying after a soft reset before giving up on the board
 * coming back. Rejoining a network it is already associated with is quick,
 * but a busy access point can take a few seconds. */
const RECONNECT_TIMEOUT_MS = 20000;
const RECONNECT_INTERVAL_MS = 1000;

const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

class Esp32MicroPythonWifi {
    /**
     * @param {string} address - the board's address on the network.
     * @param {object} config - the device options from the VM.
     * @param {string} userDataPath - where to stage the generated program.
     * @param {string} toolsPath - unused here; kept so both uploaders take the
     *   same arguments and the session does not have to care which it has.
     * @param {Function} sendstd - writes a line to the upload console.
     * @param {Function} sendRemoteRequest - talks back to the VM.
     */
    constructor (address, config, userDataPath, toolsPath, sendstd, sendRemoteRequest) {
        this._address = address;
        this._config = config;
        this._projectPath = path.join(userDataPath, 'esp32MicroPython/project');
        this._sendstd = sendstd;
        this._sendRemoteRequest = sendRemoteRequest;

        this._abort = false;
        this._board = null;

        this._codeFilePath = path.join(this._projectPath, 'main.py');
    }

    abortUpload () {
        this._abort = true;
        if (this._board) {
            this._board.close();
            this._board = null;
        }
    }

    /**
     * Write the program to the board over WiFi and restart it.
     * @param {string} code - the MicroPython source to run on the board.
     * @returns {Promise<string>} - 'Success' or 'Aborted'.
     */
    async flash (code) {
        if (!fs.existsSync(this._projectPath)) {
            fs.mkdirSync(this._projectPath, {recursive: true});
        }
        fs.writeFileSync(this._codeFilePath, code);

        if (this._abort) return 'Aborted';

        // Whatever is running has to stop before the REPL will answer: nothing
        // reads the WebREPL socket while a program is in a loop.
        this._sendstd(`${ansi.clear}Asking ${this._address} to stop the running program...\n`);
        await sendStop(this._address);
        await delay(250);

        if (this._abort) return 'Aborted';

        this._sendstd(`${ansi.clear}Connecting to ${this._address}...\n`);
        const board = new WebRepl(this._address);
        board.setPiping(false);
        this._board = board;

        try {
            await board.open();
            await board.enterRaw();

            if (this._abort) return 'Aborted';

            await this._syncLibrary(board);

            if (this._abort) return 'Aborted';

            this._sendstd(`${ansi.clear}Sending main.py...\n`);
            await board.writeFile('main.py', fs.readFileSync(this._codeFilePath));

            this._sendstd(`${ansi.clear}Restarting board...\n`);
            await board.softReset();
        } catch (err) {
            if (this._abort) return 'Aborted';
            throw new Error(
                `${err.message}\n` +
                'The board stopped answering over WiFi. Check it is still powered and ' +
                'on the same network, or connect over USB instead.'
            );
        } finally {
            if (this._board) {
                this._board.close();
                this._board = null;
            }
        }

        this._sendstd(`${ansi.green_dark}Success\n`);
        return 'Success';
    }

    /**
     * Copy across whichever library files the board does not already have.
     * @param {WebRepl} board - an open connection, already in raw mode.
     * @returns {Promise} - resolves once the board is up to date.
     * @private
     */
    async _syncLibrary (board) {
        let installed = {};
        try {
            const raw = await board.readFile(VERSION_FILE);
            if (raw) {
                installed = JSON.parse(raw);
            }
        } catch (err) {
            // A board that has never been uploaded to, or a file we cannot
            // read: treat it as having nothing and send everything.
            installed = {};
        }

        const wanted = {};
        const stale = [];
        LIBRARY_FILES.forEach(name => {
            const contents = readLibrary(name);
            const hash = checksum(contents);
            wanted[name] = hash;
            if (installed[name] !== hash) {
                stale.push({name, contents});
            }
        });

        if (stale.length === 0) {
            this._sendstd(`${ansi.clear}Board library is already up to date.\n`);
            return;
        }

        for (const file of stale) {
            if (this._abort) return;
            this._sendstd(`${ansi.clear}Sending ${file.name} (${file.contents.length} bytes)...\n`);
            await board.writeFile(file.name, file.contents);
        }

        // Written last, so that an upload interrupted halfway through leaves
        // the board saying it has the older files -- which is true -- and the
        // next upload sends them again rather than trusting a half-done copy.
        await board.writeFile(VERSION_FILE, Buffer.from(JSON.stringify(wanted)));
    }

    /**
     * Not possible, and not a limitation of this code.
     *
     * esptool talks to the ESP32's ROM bootloader, which exists before any
     * firmware does and speaks only UART. WiFi is a radio the firmware brings
     * up, so by definition it is not there when the chip is waiting to be
     * flashed. The cable is the only way in.
     */
    flashRealtimeFirmware () {
        return Promise.reject(new Error(
            'Firmware can only be flashed over USB.\n' +
            "The chip's built-in bootloader is what receives the firmware, and it can " +
            'only be reached through the USB cable -- WiFi does not exist until the ' +
            'firmware it would be replacing is already running.\n' +
            'Connect the board with a USB cable, pick its COM port, and flash the firmware there. ' +
            'Programs can go back over WiFi afterwards.'
        ));
    }
}

/**
 * Wait for a board to come back after a restart.
 * @param {string} address - the board's address.
 * @param {Function} onProgress - called with a line for the upload console.
 * @returns {Promise<boolean>} - whether the board answered in time.
 */
const waitForBoard = async (address, onProgress = null) => {
    const deadline = Date.now() + RECONNECT_TIMEOUT_MS;
    while (Date.now() < deadline) {
        await delay(RECONNECT_INTERVAL_MS);
        const probe = new WebRepl(address);
        try {
            await probe.open();
            probe.close();
            return true;
        } catch (err) {
            probe.close();
            if (onProgress) {
                onProgress();
            }
        }
    }
    return false;
};

module.exports = Esp32MicroPythonWifi;
module.exports.waitForBoard = waitForBoard;
module.exports.LIBRARY_FILES = LIBRARY_FILES;
