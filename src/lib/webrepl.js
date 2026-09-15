const WebSocket = require('ws');

/**
 * A client for MicroPython's WebREPL: the same REPL that comes down the USB
 * cable, carried over a websocket instead.
 *
 * Two things use it. Live ("Arena") mode just pipes bytes through, exactly as
 * the serial port does, so the blocks upstream cannot tell the difference. The
 * uploader drives the raw REPL underneath -- the same protocol mpremote speaks
 * -- to write files and reset the board.
 *
 * Note what this class deliberately does not do: WebREPL's own binary
 * file-transfer protocol. Chunked writes through the raw REPL are a little
 * slower but they are the same code path as the serial uploader, and there is
 * only one protocol to get wrong instead of two.
 */

const CTRL_A = '\x01';
const CTRL_B = '\x02';
const CTRL_C = '\x03';
const CTRL_D = '\x04';

/** What the board prints once it is in raw mode. */
const RAW_BANNER = 'raw REPL; CTRL-B to exit';

/** Default WebREPL port, matching mieowifi.WEBREPL_PORT on the board. */
const DEFAULT_PORT = 8266;

/** Must match mieowifi.DEFAULT_PASSWORD. */
const DEFAULT_PASSWORD = 'mieo';

/** Bytes of file content per write. Base64 makes each line about a third
 * longer again, which the board's line buffer takes comfortably. */
const CHUNK_SIZE = 1024;

const OPEN_TIMEOUT_MS = 8000;
const EXEC_TIMEOUT_MS = 10000;

const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

class WebRepl {
    /**
     * @param {string} host - the board's address on the network.
     * @param {object} options - port, password, and a callback for incoming bytes.
     */
    constructor (host, options = {}) {
        this._host = host;
        this._port = options.port || DEFAULT_PORT;
        this._password = options.password || DEFAULT_PASSWORD;

        this._socket = null;
        this._buffer = '';
        this._waiter = null;
        this._onData = options.onData || null;
        this._onClose = options.onClose || null;

        // While the uploader is driving the raw REPL, incoming bytes are its
        // business alone: passing them upstream as well would show the whole
        // file transfer in the console.
        this._piping = true;
    }

    get address () {
        return this._host;
    }

    /**
     * @param {?Function} callback - called with a Buffer for every byte the board sends.
     */
    setOnData (callback) {
        this._onData = callback;
    }

    /**
     * Connect and get past the password prompt.
     * @returns {Promise} - resolves once the board is at its REPL.
     */
    open () {
        return new Promise((resolve, reject) => {
            let settled = false;
            const fail = err => {
                if (settled) return;
                settled = true;
                this._teardown();
                reject(err);
            };

            const timer = setTimeout(
                () => fail(new Error(`No answer from ${this._host} on port ${this._port}.`)),
                OPEN_TIMEOUT_MS
            );

            let socket;
            try {
                socket = new WebSocket(`ws://${this._host}:${this._port}`);
            } catch (err) {
                clearTimeout(timer);
                return fail(err);
            }
            this._socket = socket;

            socket.on('error', err => {
                clearTimeout(timer);
                fail(new Error(`Could not reach ${this._host}: ${err.message}`));
            });

            socket.on('close', () => {
                clearTimeout(timer);
                if (!settled) {
                    return fail(new Error(`${this._host} closed the connection.`));
                }
                this._rejectWaiter(new Error('The board closed the connection.'));
                if (this._onClose) {
                    this._onClose();
                }
            });

            socket.on('message', data => {
                this._receive(Buffer.from(data));
            });

            socket.on('open', () => {
                // MicroPython asks for the password before anything else, and
                // wants it terminated with a bare CR -- the same exchange
                // webrepl_cli.py performs.
                this._expect('Password: ', OPEN_TIMEOUT_MS)
                    .then(() => {
                        this._send(`${this._password}\r`);
                        return this._expectAny(['WebREPL connected', 'denied'], OPEN_TIMEOUT_MS);
                    })
                    .then(({matched}) => {
                        if (matched === 'denied') {
                            throw new Error(
                                `${this._host} refused the WebREPL password. ` +
                                'Upload once over USB to set it again.'
                            );
                        }
                        clearTimeout(timer);
                        settled = true;
                        resolve();
                    })
                    .catch(fail);
            });
        });
    }

    /**
     * Send bytes to the board untouched. This is what live mode uses.
     * @param {Buffer} buffer - the bytes to send.
     * @returns {number} - how many bytes were sent.
     */
    send (buffer) {
        if (!this.isOpen()) return 0;
        this._socket.send(buffer);
        return buffer.length;
    }

    isOpen () {
        return this._socket !== null && this._socket.readyState === WebSocket.OPEN;
    }

    close () {
        this._teardown();
    }

    // ------------------------------------------------------------- raw REPL

    /**
     * Put the board into raw mode, where it takes code without echoing it.
     *
     * The Ctrl-C here only lands if the board is already at a prompt. A running
     * program has to be stopped over UDP first (see mieowifi), because nothing
     * reads this socket while user code is in a loop.
     * @returns {Promise} - resolves once the board is in raw mode.
     */
    async enterRaw () {
        for (let attempt = 0; attempt < 2; attempt++) {
            this._send(`\r${CTRL_C}${CTRL_C}`);
            await delay(150);
            this._buffer = '';
            this._send(`\r${CTRL_A}`);
            try {
                await this._expect(RAW_BANNER, 3000);
                await this._expect('>', 3000);
                return;
            } catch (err) {
                if (attempt === 1) {
                    throw new Error(
                        `${this._host} did not answer its REPL. ` +
                        'It may still be busy running a program.'
                    );
                }
            }
        }
    }

    /**
     * Run a snippet on the board and wait for it to finish.
     * @param {string} code - the MicroPython source to run.
     * @returns {Promise<string>} - whatever the snippet printed.
     */
    async exec (code) {
        this._send(code + CTRL_D);
        await this._expect('OK', EXEC_TIMEOUT_MS);
        const out = await this._expect(CTRL_D, EXEC_TIMEOUT_MS);
        const err = await this._expect(CTRL_D, EXEC_TIMEOUT_MS);
        await this._expect('>', EXEC_TIMEOUT_MS);
        if (err.trim()) {
            throw new Error(err.trim());
        }
        return out;
    }

    /**
     * Write a file onto the board's filesystem.
     *
     * Sent as base64 in chunks, each its own statement, so the board never has
     * to hold more than one chunk in memory at a time -- the panel library is
     * larger than the free heap on a busy board.
     * @param {string} remoteName - the name to save it under.
     * @param {Buffer} contents - the bytes to write.
     * @returns {Promise} - resolves once the file is closed.
     */
    async writeFile (remoteName, contents) {
        const target = JSON.stringify(remoteName);
        await this.exec(`import ubinascii\n_f=open(${target},'wb')`);
        for (let offset = 0; offset < contents.length; offset += CHUNK_SIZE) {
            const chunk = contents.slice(offset, offset + CHUNK_SIZE).toString('base64');
            await this.exec(`_f.write(ubinascii.a2b_base64('${chunk}'))`);
        }
        await this.exec('_f.close()\ndel _f');
    }

    /**
     * Read a small file off the board.
     * @param {string} remoteName - the file to read.
     * @returns {Promise<?string>} - its contents, or null if it is not there.
     */
    async readFile (remoteName) {
        const target = JSON.stringify(remoteName);
        const out = await this.exec(
            'try:\n' +
            `    _f=open(${target})\n` +
            '    print(_f.read(), end="")\n' +
            '    _f.close()\n' +
            '    del _f\n' +
            'except OSError:\n' +
            '    print(chr(0), end="")\n'
        );
        return out === '\x00' ? null : out;
    }

    /**
     * Leave raw mode and soft-reset, so boot.py and main.py run again.
     *
     * The websocket does not survive this -- the board closes every socket on
     * reset -- so the caller has to reconnect afterwards.
     */
    async softReset () {
        this._send(CTRL_B);
        await delay(80);
        this._send(CTRL_D);
        await delay(120);
    }

    // -------------------------------------------------------------- plumbing

    /**
     * @param {boolean} piping - whether incoming bytes should also go upstream.
     */
    setPiping (piping) {
        this._piping = piping;
    }

    _send (text) {
        if (this.isOpen()) {
            this._socket.send(Buffer.from(text, 'latin1'));
        }
    }

    _receive (buffer) {
        if (this._piping && this._onData) {
            this._onData(buffer);
        }
        this._buffer += buffer.toString('latin1');
        this._checkWaiter();
    }

    /**
     * Wait for `needle` to arrive, and hand back everything that came before it.
     * Both the text before it and the needle itself are taken off the buffer.
     * @param {string} needle - the text to wait for.
     * @param {number} timeout - how long to wait, in milliseconds.
     * @returns {Promise<string>} - the text that preceded the needle.
     * @private
     */
    _expect (needle, timeout) {
        return this._expectAny([needle], timeout).then(result => result.before);
    }

    /**
     * Wait for whichever of several strings turns up first.
     * @param {Array<string>} needles - the alternatives to wait for.
     * @param {number} timeout - how long to wait, in milliseconds.
     * @returns {Promise<{before: string, matched: string}>} - the text that
     *   preceded the match, and which alternative matched.
     * @private
     */
    _expectAny (needles, timeout) {
        return new Promise((resolve, reject) => {
            if (this._waiter) {
                return reject(new Error('a read is already in progress'));
            }
            this._waiter = {
                needles,
                resolve,
                reject,
                timer: setTimeout(() => {
                    this._rejectWaiter(new Error(
                        'Timed out waiting for the board to answer ' +
                        `(expected ${needles.map(n => JSON.stringify(n)).join(' or ')}).`
                    ));
                }, timeout)
            };
            this._checkWaiter();
        });
    }

    _checkWaiter () {
        const waiter = this._waiter;
        if (!waiter) return;

        // Whichever alternative appears earliest in the buffer wins, so that
        // the answer does not depend on the order they were listed in.
        let best = null;
        waiter.needles.forEach(needle => {
            const index = this._buffer.indexOf(needle);
            if (index >= 0 && (best === null || index < best.index)) {
                best = {index, needle};
            }
        });
        if (best === null) return;

        const before = this._buffer.slice(0, best.index);
        this._buffer = this._buffer.slice(best.index + best.needle.length);
        this._waiter = null;
        clearTimeout(waiter.timer);
        waiter.resolve({before, matched: best.needle});
    }

    _rejectWaiter (err) {
        const waiter = this._waiter;
        if (!waiter) return;
        this._waiter = null;
        clearTimeout(waiter.timer);
        waiter.reject(err);
    }

    _teardown () {
        if (this._socket) {
            const socket = this._socket;
            this._socket = null;
            socket.removeAllListeners();
            try {
                socket.close();
            } catch (err) {
                // Already gone.
            }
        }
        this._rejectWaiter(new Error('The connection was closed.'));
        this._buffer = '';
    }
}

module.exports = WebRepl;
module.exports.DEFAULT_PORT = DEFAULT_PORT;
module.exports.DEFAULT_PASSWORD = DEFAULT_PASSWORD;
