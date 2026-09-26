const fs = require('fs');
const {spawn, spawnSync} = require('child_process');
const path = require('path');
const ansi = require('ansi-string');
const os = require('os');

const {LIBRARY_FILES, VERSION_FILE, libraryPath, manifest} = require('../lib/mieo-library');

const ABORT_STATE_CHECK_INTERVAL = 100;

// How long to keep asking a freshly restarted board whether it is up yet.
// After a firmware flash its first boot also formats the filesystem, which
// is much the slowest start it ever has.
const BOARD_BOOT_TIMEOUT = 30000;

const ESPTOOL_MODULE_NAME = 'esptool';
const MPREMOTE_MODULE_NAME = 'mpremote';

/**
 * Whether a Python can run the uploader: it must start, and import both tools
 * the uploader drives. Existing on disk is not enough -- see pickPython.
 * @param {string} py - a Python executable path or name on PATH.
 * @returns {boolean} - true if it imported esptool and mpremote.
 */
const pythonHasTools = py => {
    try {
        const result = spawnSync(py, ['-c', `import ${ESPTOOL_MODULE_NAME}, ${MPREMOTE_MODULE_NAME}`], {
            timeout: 20000,
            windowsHide: true
        });
        return result.status === 0;
    } catch (err) {
        return false;
    }
};

// Cached once found: every upload builds a fresh uploader, and probing starts
// a Python process. A miss is not cached, so installing the packages mid-
// session is picked up by the next upload.
let resolvedPython = null;

/**
 * Choose the Python to drive esptool and mpremote with.
 *
 * The bundled one is preferred, but only if it actually works. It used to be
 * chosen merely for existing, and it does not work: the macOS build in
 * openblock-tools links against a Homebrew libintl at an Intel-Homebrew path
 * (/usr/local/opt/gettext) and cannot start on an Apple Silicon Mac or on any
 * Mac without that library, and no bundled build carries mpremote. Once
 * tools/ was actually packaged, that turned every Mieo upload into a crash.
 * The system Python is what works for anyone who has run
 * `pip install esptool mpremote`.
 * @param {string} bundledPyPath - where the bundled interpreter would be.
 * @returns {string} - the Python to use.
 */
const pickPython = bundledPyPath => {
    if (resolvedPython) return resolvedPython;
    const system = os.platform() === 'win32' ? ['python', 'py'] : ['python3', 'python'];
    const candidates = (fs.existsSync(bundledPyPath) ? [bundledPyPath] : []).concat(system);
    const found = candidates.find(pythonHasTools);
    if (found) {
        resolvedPython = found;
        return found;
    }
    // Nothing has both packages. Use the system Python, so the failure the
    // user sees is the actionable "pip install esptool mpremote" rather than a
    // crash inside a bundled interpreter they cannot repair.
    return system[0];
};

// Standard flash offset for the MicroPython bootloader/app image on the
// original ESP32 (WROOM-32 / WROOM-32E / WROVER). Other Espressif chips
// (S2/S3/C3...) use a different offset, but those are separate devices.
const FIRMWARE_FLASH_ADDRESS = '0x1000';

class Esp32MicroPython {
    constructor (peripheralPath, config, userDataPath, toolsPath, sendstd, sendRemoteRequest) {
        this._peripheralPath = peripheralPath;
        this._config = config;
        this._userDataPath = userDataPath;
        this._projectPath = path.join(userDataPath, 'esp32MicroPython/project');
        // openblock-firmwares ships every MicroPython image in one directory,
        // so that is where the .bin is looked for. `npm run fetch:firmwares`
        // populates it; nothing has to be placed there by hand.
        this._firmwareDir = path.join(toolsPath, '../firmwares/microPython');
        this._sendstd = sendstd;
        this._sendRemoteRequest = sendRemoteRequest;

        this._abort = false;
        this._activeProcess = null;

        // Prefer a bundled Python (shipped alongside the other board toolchains)
        // when it actually works; otherwise the system one. See pickPython.
        const pythonDir = path.join(toolsPath, 'Python');
        let bundledPyPath;
        if (os.platform() === 'darwin') {
            bundledPyPath = path.join(pythonDir, 'python3');
        } else if (os.platform() === 'linux') {
            bundledPyPath = path.join(pythonDir, 'bin/python3');
        } else {
            bundledPyPath = path.join(pythonDir, 'python.exe');
        }
        this._pyPath = pickPython(bundledPyPath);

        // Made here rather than in flash(): flashRealtimeFirmware() also writes
        // into it, and a profile whose very first action is "Upload Firmware"
        // would otherwise fail to record the library manifest, making every
        // later Bluetooth upload re-send the whole library.
        if (!fs.existsSync(this._projectPath)) {
            fs.mkdirSync(this._projectPath, {recursive: true});
        }

        this._codeFilePath = path.join(this._projectPath, 'main.py');
        this._manifestFilePath = path.join(this._projectPath, VERSION_FILE);
    }

    abortUpload () {
        this._abort = true;
        if (this._activeProcess) {
            if (os.platform() === 'win32') {
                spawnSync('taskkill', ['/pid', this._activeProcess.pid, '/f', '/t']);
            } else {
                this._activeProcess.kill();
            }
        }
    }

    /**
     * Spawn `python -m <module> ...args` and stream stdout/stderr to the upload console.
     * @param {Array<string>} args - arguments passed to the python interpreter.
     * @returns {Promise<string>} - resolves 'Success' or 'Aborted', rejects with an Error on failure.
     * @private
     */
    _run (args, quiet = false) {
        return new Promise((resolve, reject) => {
            let proc;
            try {
                proc = spawn(this._pyPath, args);
            } catch (err) {
                return reject(new Error(`Could not start python (${this._pyPath}): ${err.message}`));
            }
            this._activeProcess = proc;

            let stderrBuf = '';

            proc.stdout.on('data', buf => {
                if (!quiet) {
                    this._sendstd(buf.toString());
                }
            });
            proc.stderr.on('data', buf => {
                const data = buf.toString();
                stderrBuf += data;
                // Held back while quiet: an attempt that is about to be retried
                // should not put a traceback on screen for a problem that then
                // goes away by itself.
                if (!quiet) {
                    this._sendstd(ansi.red + data);
                }
            });

            proc.on('error', err => {
                this._activeProcess = null;
                reject(new Error(
                    `Failed to run "python -m ${args[1]}": ${err.message}\n` +
                    'Make sure Python 3 and the esptool/mpremote packages are installed ' +
                    '(pip install esptool mpremote) and on PATH.'
                ));
            });

            proc.on('exit', code => {
                this._activeProcess = null;
                if (this._abort) {
                    return resolve('Aborted');
                }
                if (code === 0) {
                    return resolve('Success');
                }
                return reject(new Error(`"python -m ${args[1]}" exited with code ${code}\n${stderrBuf}`));
            });
        });
    }

    /**
     * Run an mpremote command, giving it a few goes before believing it.
     *
     * Talking to a board that has just restarted is racy in a way that says
     * nothing about whether it works: the command can arrive while the boot
     * banner is still being printed, and be lost in it.
     * @param {Array<string>} args - arguments for the python interpreter.
     * @param {number} attempts - how many times to try before giving up.
     * @returns {Promise<string>} - 'Success' or 'Aborted'.
     * @private
     */
    async _runWithRetry (args, attempts = 3) {
        let lastError = null;
        for (let attempt = 1; attempt <= attempts; attempt++) {
            if (this._abort) {
                return 'Aborted';
            }
            try {
                return await this._run(args, attempt < attempts);
            } catch (err) {
                lastError = err;
                if (attempt < attempts) {
                    await new Promise(resolve => setTimeout(resolve, 1200));
                }
            }
        }
        throw lastError;
    }

    /**
     * Wait until the board answers its prompt.
     *
     * esptool resets the board on its way out, and opening the port resets it
     * again -- so anything said in the first second or two arrives in the
     * middle of a boot banner and is lost, which mpremote reports as "could
     * not enter raw repl". Rather than guess how long a board takes to start,
     * ask it until it answers.
     * @returns {Promise<string>} - 'Success' or 'Aborted'.
     * @private
     */
    async _waitForRepl () {
        const deadline = Date.now() + BOARD_BOOT_TIMEOUT;
        let lastError = null;

        while (Date.now() < deadline) {
            if (this._abort) {
                return 'Aborted';
            }
            try {
                await this._run([
                    '-m', MPREMOTE_MODULE_NAME,
                    'connect', `port:${this._peripheralPath}`, 'resume',
                    'exec', 'pass'
                ], true);
                this._sendstd('\n');
                return 'Success';
            } catch (err) {
                lastError = err;
                this._sendstd('.');
                await new Promise(resolve => setTimeout(resolve, 1500));
            }
        }

        throw new Error(
            `${lastError ? lastError.message : ''}\n` +
            'The board did not reach its prompt after restarting. Unplug it, plug it back ' +
            'in, and press Upload.'
        );
    }

    /**
     * Put the board-side library on the board.
     *
     * The whole library goes down the cable every time. It is only a moment
     * over USB, and it means a board that has been anywhere near an older
     * version of Robolab is put right by one upload.
     * @returns {Promise<string>} - 'Success' or 'Aborted'.
     */
    async installLibrary () {
        // One mpremote for the whole library, not one per file. Each
        // invocation costs a Python startup, a port open and a REPL
        // handshake -- far more than the copying itself -- so nine of them
        // was most of the wait, and the board only needs telling once.
        const files = LIBRARY_FILES.slice();

        // The manifest rides along in the same batch. It records what the
        // board now holds, so a later wireless upload can skip what has not
        // moved; written first so it is part of the one transfer.
        let manifestIncluded = false;
        try {
            fs.writeFileSync(this._manifestFilePath, JSON.stringify(manifest()));
            manifestIncluded = true;
        } catch (err) {
            this._sendstd(`${ansi.yellow_dark}Could not record the library version: ${err.message}\n`);
        }

        this._sendstd(`${ansi.clear}Copying the Mieo library to the board over ${this._peripheralPath}` +
            ` (${files.length + (manifestIncluded ? 1 : 0)} files)...\n`);

        const sources = files.map(name => libraryPath(name));
        if (manifestIncluded) {
            sources.push(this._manifestFilePath);
        }

        try {
            const copyResult = await this._runWithRetry([
                '-m', MPREMOTE_MODULE_NAME,
                'connect', `port:${this._peripheralPath}`, 'resume',
                'fs', 'cp'
            ].concat(sources).concat([':']));
            if (copyResult === 'Aborted') {
                return 'Aborted';
            }
        } catch (err) {
            throw new Error(
                `${err.message}\n` +
                'Could not write the Mieo library to the board. If this is a fresh ESP32, flash ' +
                'the MicroPython firmware first with the "Upload Firmware" button.'
            );
        }

        return this._abort ? 'Aborted' : 'Success';
    }

    /**
     * Ask the board whether Bluetooth actually came up, and say so.
     *
     * Worth doing out loud: if this is quiet, the board simply never appears in
     * the connection window and there is nothing to look at to find out why.
     * @returns {Promise} - resolves once the board has answered or failed to.
     */
    async reportBluetooth () {
        this._sendstd(`${ansi.clear}Starting Bluetooth on the board...\n`);
        try {
            await this._runWithRetry([
                '-m', MPREMOTE_MODULE_NAME,
                'connect', `port:${this._peripheralPath}`, 'resume',
                'exec', 'import mieoble; print("Bluetooth on, advertising as", mieoble.start())'
            ]);
        } catch (err) {
            this._sendstd(
                `${ansi.red}Bluetooth did not start on the board:\n${err.message}\n` +
                'The board will still work over USB. If this mentions "bluetooth", the ' +
                'MicroPython firmware on this board was built without it.\n'
            );
        }
    }

    /**
     * Copy the generated MicroPython code onto the board as main.py using mpremote.
     * @param {string} code - the MicroPython source to run on the board.
     * @returns {Promise<string>} - 'Success' or 'Aborted'.
     */
    async flash (code) {
        if (!fs.existsSync(this._projectPath)) {
            fs.mkdirSync(this._projectPath, {recursive: true});
        }
        try {
            fs.writeFileSync(this._codeFilePath, code);
        } catch (err) {
            return Promise.reject(err);
        }

        if (this._abort) {
            return 'Aborted';
        }

        const libraryResult = await this.installLibrary();
        if (libraryResult === 'Aborted') {
            return 'Aborted';
        }

        // Says out loud whether the board will be reachable without the cable
        // after this. Cheap, and the only place that question gets answered.
        await this.reportBluetooth();

        if (this._abort) {
            return 'Aborted';
        }

        this._sendstd(`${ansi.clear}Checking mieo.py imports on the board...\n`);
        try {
            await this._run([
                '-m', MPREMOTE_MODULE_NAME,
                'connect', `port:${this._peripheralPath}`, 'resume',
                'exec', 'import mieo'
            ]);
        } catch (err) {
            // Not fatal to the upload -- main.py still gets written below -- but the
            // display blocks silently do nothing on the board without a working
            // mieo.py/display.py, so surface the traceback here instead of leaving it invisible.
            this._sendstd(
                `${ansi.red}mieo.py failed to import on the board:\n${err.message}\n` +
                'The display blocks will not work until this is fixed.\n'
            );
        }

        if (this._abort) {
            return 'Aborted';
        }

        this._sendstd(`${ansi.clear}Copying main.py to the board over ${this._peripheralPath}...\n`);
        try {
            const copyResult = await this._run([
                '-m', MPREMOTE_MODULE_NAME,
                'connect', `port:${this._peripheralPath}`, 'resume',
                'fs', 'cp', this._codeFilePath, ':main.py'
            ]);
            if (copyResult === 'Aborted') {
                return 'Aborted';
            }
        } catch (err) {
            throw new Error(
                `${err.message}\n` +
                'Could not write the program to the board. If this is a fresh ESP32, flash the ' +
                'MicroPython firmware first with the "Upload Firmware" button.'
            );
        }

        if (this._abort) {
            return 'Aborted';
        }

        this._sendstd(`${ansi.clear}Restarting board...\n`);
        try {
            // A hard reset, not a soft one. Soft-resetting an ESP32 whose radio
            // is up panics this MicroPython build, and the board's own boot.py
            // brings the radio up -- so a soft reset here would crash the board
            // on the way out of every upload after the first.
            await this._run([
                '-m', MPREMOTE_MODULE_NAME,
                'connect', `port:${this._peripheralPath}`, 'resume', 'resume',
                'exec', '--no-follow', 'import machine; machine.reset()'
            ]);
        } catch (err) {
            // A failed restart does not mean the upload failed: the program is
            // already on the board and runs when it next starts.
            this._sendstd(`${ansi.yellow_dark}Could not restart the board: ${err.message}\n`);
        }

        this._sendstd(`${ansi.green_dark}Success\n`);
        return 'Success';
    }

    /**
     * Flash the MicroPython firmware image onto the ESP32 with esptool.
     * @returns {Promise<string>} - 'Success' or 'Aborted'.
     */
    async flashRealtimeFirmware () {
        if (!this._config.firmware) {
            throw new Error('No MicroPython firmware file is configured for this board.');
        }

        const firmwarePath = path.join(this._firmwareDir, this._config.firmware);
        if (!fs.existsSync(firmwarePath)) {
            throw new Error(
                `MicroPython firmware not found at ${firmwarePath}.\n` +
                'Download the ESP32 (WROOM-32/32E) .bin build from ' +
                'https://micropython.org/download/ESP32_GENERIC/ and place it there.'
            );
        }

        const baud = '460800';
        const chip = 'esp32';

        this._sendstd(`${ansi.clear}Erasing flash on ${this._peripheralPath}...\n`);
        const eraseResult = await this._run([
            '-m', ESPTOOL_MODULE_NAME,
            '--chip', chip,
            '--port', this._peripheralPath,
            '--baud', baud,
            'erase-flash'
        ]);
        if (eraseResult === 'Aborted') {
            return 'Aborted';
        }

        this._sendstd(`${ansi.clear}Writing MicroPython firmware (this can take a minute)...\n`);
        const writeResult = await this._run([
            '-m', ESPTOOL_MODULE_NAME,
            '--chip', chip,
            '--port', this._peripheralPath,
            '--baud', baud,
            'write-flash',
            '--flash-mode', 'dio',
            '--flash-size', 'detect',
            FIRMWARE_FLASH_ADDRESS, firmwarePath
        ]);
        if (writeResult === 'Aborted') {
            return 'Aborted';
        }

        this._sendstd(`${ansi.green_dark}Firmware flashed successfully.\n`);

        // erase-flash above wiped the filesystem along with the old firmware,
        // so the board is now bare: no library, no boot.py, and therefore no
        // Bluetooth. Putting it back here is the difference between a board
        // that works when the cable comes out and one that silently does not.
        this._sendstd(`${ansi.clear}Reinstalling the Mieo library (the firmware flash erased it)...\n`);

        // The board has just rebooted into fresh MicroPython. Wait for it to
        // actually say so: a fixed pause cannot work here, because opening the
        // port resets the board a second time, after the pause has passed.
        this._sendstd(`${ansi.clear}Waiting for the board to start`);
        const ready = await this._waitForRepl();
        if (ready === 'Aborted') {
            return 'Aborted';
        }

        try {
            const libraryResult = await this.installLibrary();
            if (libraryResult === 'Aborted') {
                return 'Aborted';
            }
            await this.reportBluetooth();
        } catch (err) {
            this._sendstd(
                `${ansi.red}The firmware is flashed, but the Mieo library could not be ` +
                `installed:\n${err.message}\n` +
                'Press the upload button to try again.\n'
            );
            return 'Success';
        }

        this._sendstd(`${ansi.green_dark}Board ready.\n`);
        return 'Success';
    }
}

module.exports = Esp32MicroPython;
