const {SerialPort} = require('serialport');
const ansi = require('ansi-string');

const Session = require('./session');
const Arduino = require('../upload/arduino');
const Microbit = require('../upload/microbit');
const Esp32MicroPython = require('../upload/esp32MicroPython');
const Esp32MicroPythonWifi = require('../upload/esp32MicroPythonWifi');
const MieoDiscovery = require('../lib/mieo-discovery');
const WebRepl = require('../lib/webrepl');

const {waitForBoard} = Esp32MicroPythonWifi;
const {sendStop} = MieoDiscovery;

const usbId = require('../lib/usb-id');

const PERIPHERAL_UNPLUG_CHECK_INTERVAL = 100;

/**
 * Marks a peripheral id as an address on the network rather than a COM port.
 *
 * Boards reachable over WiFi are offered in the same list as boards on a
 * cable, because from the editor's point of view there is no difference: it
 * picks one, writes to it, and uploads to it. Everything that has to know
 * which kind it got looks at this prefix.
 * @readonly
 */
const WIFI_PREFIX = 'wifi:';

/** The byte the editor sends to interrupt whatever the board is doing. */
const CTRL_C = 0x03;

/** How close together two interrupts have to be to count as the same one. */
const WIFI_INTERRUPT_INTERVAL = 500;

class SerialportSession extends Session {
    constructor (socket, userDataPath, toolsPath) {
        super(socket);

        this.userDataPath = userDataPath;
        this.toolsPath = toolsPath;

        this._type = 'serialport';
        this.peripheral = null;
        this.peripheralParams = null;
        this.services = null;
        this.reportedPeripherals = {};
        this.connectStateDetectorTimer = null;
        this.peripheralsScanorTimer = null;
        this.isRead = false;
        this.isInDisconnect = false;
        this.tool = null;

        /** An open WebREPL connection, when the chosen board is on WiFi. */
        this.wifi = null;
        this.wifiDiscovery = null;
        this.lastWifiInterrupt = 0;
    }

    async didReceiveCall (method, params, completion) {
        switch (method) {
        case 'discover':
            this.discover(params);
            completion(null, null);
            break;
        case 'connect':
            await this.connect(params);
            completion(null, null);
            break;
        case 'disconnect':
            await this.disconnect();
            completion(null, null);
            break;
        case 'updateBaudrate':
            completion(await this.updateBaudrate(params), null);
            break;
        case 'write':
            completion(await this.write(params), null);
            break;
        case 'read':
            await this.read(params);
            completion(null, null);
            break;
        case 'upload':
            completion(await this.upload(params), null);
            break;
        case 'uploadFirmware':
            completion(await this.uploadFirmware(params), null);
            break;
        case 'abortUpload':
            completion(await this.abortUpload(), null);
            break;
        case 'getServices':
            completion((this.services || []).map(service => service.uuid), null);
            break;
        case 'pingMe':
            completion('willPing', null);
            this.sendRemoteRequest('ping', null, result => {
                console.log(`Got result from ping: ${result}`);
            });
            break;
        default:
            throw new Error(`Method not found`);
        }
    }

    discover (params) {
        if (this.services) {
            throw new Error('cannot discover when connected');
        }
        const {filters} = params;
        if (!Array.isArray(filters.pnpid) || filters.pnpid.length < 1) {
            throw new Error('discovery request must include filters');
        }
        this.reportedPeripherals = {};

        this.peripheralsScanorTimer = setInterval(() => {
            SerialPort.list().then(peripheral => {
                this.onAdvertisementReceived(peripheral, filters);
            });
        }, 100);

        // Only boards that say they can do this get looked for over the
        // network, so a micro:bit page never offers a Mieo it happens to hear.
        if (filters.wifi) {
            this.discoverOverWifi();
        }
    }

    /**
     * Look for boards on the local network and offer them like any other.
     *
     * Boards answer a broadcast rather than announcing themselves, so the same
     * board replies to every probe; reporting it again each time is harmless
     * and is what keeps a board that was switched on late from being missed.
     */
    discoverOverWifi () {
        if (this.wifiDiscovery) return;

        this.wifiDiscovery = new MieoDiscovery();
        this.wifiDiscovery.start(({address, name}) => {
            const peripheralId = `${WIFI_PREFIX}${address}`;
            this.reportedPeripherals[peripheralId] = {wifi: true, address, name};
            this.sendRemoteRequest('didDiscoverPeripheral', {
                peripheralId,
                name: `${name} (WiFi ${address})`
            });
        });
    }

    stopWifiDiscovery () {
        if (this.wifiDiscovery) {
            this.wifiDiscovery.stop();
            this.wifiDiscovery = null;
        }
    }

    onAdvertisementReceived (peripheral, filters) {
        if (peripheral) {
            peripheral.forEach(device => {
                const vendorId = String(device.vendorId).toUpperCase();
                const productId = String(device.productId).toUpperCase();
                const pnpid = `USB\\VID_${vendorId}&PID_${productId}`;

                const name = usbId[pnpid] ? usbId[pnpid] : 'Unknown device';

                if (filters.pnpid.includes('*')) {
                    this.reportedPeripherals[device.path] = device;
                    this.sendRemoteRequest('didDiscoverPeripheral', {
                        peripheralId: device.path,
                        name: `${name} (${device.path})`
                    });
                } else if (filters.pnpid.includes(pnpid)) {
                    this.reportedPeripherals[device.path] = device;
                    this.sendRemoteRequest('didDiscoverPeripheral', {
                        peripheralId: device.path,
                        name: `${name} (${device.path})`
                    });
                }
            });
        }
    }

    connect (params, isConnectAfterUpload = false) {
        if (String(params.peripheralId).startsWith(WIFI_PREFIX)) {
            return this.connectOverWifi(params, isConnectAfterUpload);
        }
        return new Promise((resolve, reject) => {
            if (this.peripheral && this.peripheral.isOpen === true) {
                return reject(new Error('already connected to peripheral'));
            }
            const {peripheralId, peripheralConfig} = params;

            const peripheral = this.reportedPeripherals[peripheralId];
            if (!peripheral) {
                return reject(new Error(`invalid peripheral ID: ${peripheralId}`));
            }
            if (this.peripheralsScanorTimer) {
                clearInterval(this.peripheralsScanorTimer);
                this.peripheralsScanorTimer = null;
            }
            this.stopWifiDiscovery();
            const port = new SerialPort({
                path: peripheral.path,
                baudRate: peripheralConfig.config.baudRate,
                dataBits: peripheralConfig.config.dataBits,
                stopBits: peripheralConfig.config.stopBits,
                autoOpen: false
            });
            const rts = (typeof peripheralConfig.config.rts === 'undefined') ? true : peripheralConfig.config.rts;
            const dtr = (typeof peripheralConfig.config.dtr === 'undefined') ? true : peripheralConfig.config.dtr;

            try {
                port.open(openErr => {
                    if (openErr) {
                        if (isConnectAfterUpload === true) {
                            this.sendRemoteRequest('uploadError', {
                                message: ansi.red + openErr.message
                            });
                            this.sendRemoteRequest('peripheralUnplug', null);
                        }
                        if (openErr.message.includes('Access denied')) {
                            this.sendRemoteRequest('connectError', {message: 'Access denied'});
                        }
                        if (openErr.message.includes('Open (SetCommState): Unknown error code 31')) {
                            this.sendRemoteRequest('connectError', {message: 'Unknown error code 31'});
                        }
                        return reject(new Error(openErr));
                    }

                    port.set({rts: rts, dtr: dtr}, setErr => {
                        if (setErr) {
                            if (isConnectAfterUpload === true) {
                                this.sendRemoteRequest('peripheralUnplug', null);
                            }
                            return reject(new Error(setErr));
                        }

                        this.peripheral = port;
                        this.peripheralParams = params;

                        // Scan COM status prevent device pulled out
                        this.connectStateDetectorTimer = setInterval(() => {
                            if (this.peripheral.isOpen === false) {
                                clearInterval(this.connectStateDetectorTimer);
                                this.disconnect();
                                this.sendRemoteRequest('peripheralUnplug', null);
                            }
                        }, PERIPHERAL_UNPLUG_CHECK_INTERVAL);

                        // Only when the receiver function is set, can isopen detect that the device is pulled out
                        // A strange features of npm serialport package
                        port.on('data', rev => {
                            this.onMessageCallback(rev);
                        });

                        port.on('error', error => {
                            console.log('OpenBlock Link Error:', error);
                            this.disconnect();
                            this.sendRemoteRequest('peripheralUnplug', null);
                        });

                        resolve();
                    });
                });
            } catch (err) {
                reject(err);
            }
        });
    }

    /**
     * Open a WebREPL connection to a board on the network.
     *
     * The address comes out of the peripheral id rather than the scan results,
     * so reconnecting after an upload works even though the scan stopped when
     * the board was first chosen.
     * @param {object} params - the peripheral id and config from the editor.
     * @param {boolean} isConnectAfterUpload - whether this follows an upload,
     *   in which case a failure is the upload's failure to report.
     * @returns {Promise} - resolves once the board is at its prompt.
     */
    async connectOverWifi (params, isConnectAfterUpload = false) {
        if (this.wifi) {
            throw new Error('already connected to peripheral');
        }

        if (this.peripheralsScanorTimer) {
            clearInterval(this.peripheralsScanorTimer);
            this.peripheralsScanorTimer = null;
        }
        this.stopWifiDiscovery();

        const address = String(params.peripheralId).slice(WIFI_PREFIX.length);
        const board = new WebRepl(address, {
            onData: rev => this.onMessageCallback(rev),
            onClose: () => {
                // A board that goes off the network is gone in exactly the way
                // an unplugged one is, and the editor already knows what to do
                // about that.
                if (this.isInDisconnect || this.wifi !== board) return;
                this.wifi = null;
                this.sendRemoteRequest('peripheralUnplug', null);
            }
        });

        try {
            await board.open();
        } catch (err) {
            board.close();
            if (isConnectAfterUpload === true) {
                this.sendRemoteRequest('uploadError', {message: ansi.red + err.message});
                this.sendRemoteRequest('peripheralUnplug', null);
            } else {
                this.sendRemoteRequest('connectError', {message: err.message});
            }
            throw err;
        }

        this.wifi = board;
        this.peripheralParams = params;
        this.isInDisconnect = false;
    }

    /**
     * Turn a Ctrl-C meant for the board into something WiFi can actually deliver.
     *
     * Down a cable, Ctrl-C interrupts a running program because the serial
     * hardware notices it as it arrives. Over WiFi nothing reads the socket
     * while user code is looping, so the same byte would sit unread forever and
     * live mode would never get its prompt. The board listens for a stop on UDP
     * exactly for this, off a timer that keeps running inside a loop -- so when
     * the editor asks to interrupt, ask that way instead.
     * @param {Buffer} buffer - the bytes on their way to the board.
     */
    interruptOverWifiIfAsked (buffer) {
        if (!buffer.includes(CTRL_C)) return;

        // Live mode knocks twice in quick succession, and retries; one stop
        // covers the lot.
        const now = Date.now();
        if (now - this.lastWifiInterrupt < WIFI_INTERRUPT_INTERVAL) return;
        this.lastWifiInterrupt = now;

        sendStop(this.wifi.address).catch(() => {
            // Best effort: the Ctrl-C still goes down the socket below, which
            // is all that is needed if the board is already at its prompt.
        });
    }

    onMessageCallback (rev) {
        const params = {
            encoding: 'base64',
            message: rev.toString('base64')
        };
        if (this.isRead) {
            this.sendRemoteRequest('onMessage', params);
        }
    }

    updateBaudrate (params) {
        return new Promise((resolve, reject) => {
            // A board on WiFi has no baud rate to change; the editor sets one
            // anyway when it switches modes, and it is simply not its business.
            if (this.isInDisconnect || this.wifi) {
                return resolve();
            }
            this.peripheralParams.peripheralConfig.config.baudRate = params.baudRate;
            this.peripheral.update(params, err => {
                if (err) {
                    return reject(new Error(`Error while attempting to update baudrate: ${err.message}`));
                }

                const rts = (typeof this.peripheralParams.peripheralConfig.config.rts === 'undefined') ?
                    true : this.peripheralParams.peripheralConfig.config.rts;
                const dtr = (typeof this.peripheralParams.peripheralConfig.config.dtr === 'undefined') ?
                    true : this.peripheralParams.peripheralConfig.config.dtr;

                // After update baudrate, the rts and dtr will be automatically modified,
                // we have to set them again.
                this.peripheral.set({rts: rts, dtr: dtr}, setErr => {
                    if (setErr) {
                        this.sendRemoteRequest('peripheralUnplug', null);
                        return reject(new Error(setErr));
                    }
                    return resolve();
                });
            });

        });
    }

    write (params) {
        return new Promise((resolve, reject) => {
            const {message, encoding} = params;
            const buffer = new Buffer.from(message, encoding);

            if (this.wifi) {
                if (this.isInDisconnect) {
                    return resolve();
                }
                this.interruptOverWifiIfAsked(buffer);
                try {
                    return resolve(this.wifi.send(buffer));
                } catch (err) {
                    return reject(err);
                }
            }

            try {
                if (!this.isInDisconnect) {
                    this.peripheral.write(buffer, 'binary', err => {
                        if (err) {
                            return reject(new Error(`Error while attempting to write: ${err.message}`));
                        }
                    });
                    this.peripheral.drain(() => resolve(buffer.length));
                }
                return resolve();
            } catch (err) {
                return reject(err);
            }
        });
    }

    read () {
        this.isRead = true;
    }

    disconnect () {
        this.isInDisconnect = true;

        if (this.wifi) {
            const board = this.wifi;
            this.wifi = null;
            board.close();
            this.isInDisconnect = false;
            return Promise.resolve();
        }

        return new Promise((resolve, reject) => {
            if (this.peripheral && this.peripheral.isOpen === true) {
                if (this.connectStateDetectorTimer) {
                    clearInterval(this.connectStateDetectorTimer);
                    this.connectStateDetectorTimer = null;
                }
                const peripheral = this.peripheral;
                try {
                    peripheral.pause();
                    // clear all cache data
                    peripheral.flush(() => {
                        peripheral.close(error => {
                            if (error) {
                                this.isInDisconnect = false;
                                return reject(Error(error));
                            }
                            this.isInDisconnect = false;
                            return resolve();
                        });
                    });
                } catch (err) {
                    this.isInDisconnect = false;
                    return reject(err);
                }
            } else {
                return resolve();
            }
        });
    }

    async upload (params) {
        const {message, config, encoding} = params;
        const code = new Buffer.from(message, encoding).toString();

        if (this.wifi) {
            return this.uploadOverWifi(code, config);
        }

        const {baudRate} = this.peripheralParams.peripheralConfig.config;

        switch (config.type) {
        case 'arduino':
            this.tool = new Arduino(this.peripheral.path, config, this.userDataPath,
                this.toolsPath, this.sendstd.bind(this), this.sendRemoteRequest.bind(this));

            try {
                this.sendRemoteRequest('setUploadAbortEnabled', true);
                const exitCode = await this.tool.build(code);
                if (exitCode === 'Success') {
                    try {
                        this.sendstd(`${ansi.clear}Disconnect serial port\n`);
                        await this.disconnect();
                        this.sendstd(`${ansi.clear}Disconnected successfully, flash program starting...\n`);
                        const flashExitCode = await this.tool.flash();
                        await this.connect(this.peripheralParams, true);
                        this.sendRemoteRequest('uploadSuccess', {aborted: flashExitCode === 'Aborted'});
                    } catch (err) {
                        this.sendRemoteRequest('uploadError', {
                            message: ansi.red + err.message
                        });
                        // if error in flash step. It is considered that the device has been removed.
                        this.sendRemoteRequest('peripheralUnplug', null);
                    }
                } else if (exitCode === 'Aborted') {
                    this.sendRemoteRequest('uploadSuccess', {aborted: true});
                }
            } catch (err) {
                this.sendRemoteRequest('uploadError', {
                    message: ansi.red + err.message
                });
            }
            break;
        case 'microbit':
            this.tool = new Microbit(this.peripheral.path, config, this.userDataPath,
                this.toolsPath, this.sendstd.bind(this), this.sendRemoteRequest.bind(this));
            try {
                this.sendRemoteRequest('setUploadAbortEnabled', true);
                await this.disconnect();
                const exitCode = await this.tool.flash(code);
                await this.connect(this.peripheralParams, true);
                await this.updateBaudrate({baudRate: 115200});
                this.sendstd(`${ansi.clear}Reset device\n`);
                await this.write({message: '04', encoding: 'hex'});
                await this.updateBaudrate({baudRate: baudRate});

                this.sendRemoteRequest('uploadSuccess', {aborted: exitCode === 'Aborted'});
            } catch (err) {
                this.sendRemoteRequest('uploadError', {
                    message: ansi.red + err.message
                });
                this.sendRemoteRequest('peripheralUnplug', null);
            }
            break;
        case 'esp32MicroPython':
            this.tool = new Esp32MicroPython(this.peripheral.path, config, this.userDataPath,
                this.toolsPath, this.sendstd.bind(this), this.sendRemoteRequest.bind(this));
            try {
                this.sendRemoteRequest('setUploadAbortEnabled', true);
                this.sendstd(`${ansi.clear}Disconnect serial port\n`);
                await this.disconnect();
                this.sendstd(`${ansi.clear}Disconnected successfully, uploading code...\n`);
                const exitCode = await this.tool.flash(code);
                await this.connect(this.peripheralParams, true);
                this.sendRemoteRequest('uploadSuccess', {aborted: exitCode === 'Aborted'});
            } catch (err) {
                this.sendRemoteRequest('uploadError', {
                    message: ansi.red + err.message
                });
                this.sendRemoteRequest('peripheralUnplug', null);
            }
            break;
        }

        this.tool = null;
    }

    /**
     * Send a program to a board on the network.
     *
     * The live connection is dropped first and reopened afterwards, the same
     * shape as the serial path: the uploader needs the board's REPL to itself,
     * and the board closes every socket when it restarts at the end anyway.
     * @param {string} code - the program to run on the board.
     * @param {object} config - the device options from the VM.
     */
    async uploadOverWifi (code, config) {
        if (config.type !== 'esp32MicroPython') {
            this.sendRemoteRequest('uploadError', {
                message: `${ansi.red}This board cannot be programmed over WiFi.`
            });
            return;
        }

        // Held onto because disconnect() below is about to forget it.
        const address = this.wifi.address;
        const peripheralParams = this.peripheralParams;

        this.tool = new Esp32MicroPythonWifi(address, config, this.userDataPath,
            this.toolsPath, this.sendstd.bind(this), this.sendRemoteRequest.bind(this));

        try {
            this.sendRemoteRequest('setUploadAbortEnabled', true);
            await this.disconnect();

            const exitCode = await this.tool.flash(code);

            if (exitCode !== 'Aborted') {
                this.sendstd(`${ansi.clear}Waiting for the board to rejoin the network...\n`);
                const isBack = await waitForBoard(address, () => this.sendstd('.'));
                if (!isBack) {
                    throw new Error(
                        `${address} did not come back after restarting.\n` +
                        'The program was sent, but it may have stopped the board from rejoining ' +
                        'the network. Connect over USB to look at it.'
                    );
                }
                this.sendstd('\n');
            }

            await this.connect(peripheralParams, true);
            this.sendRemoteRequest('uploadSuccess', {aborted: exitCode === 'Aborted'});
        } catch (err) {
            this.sendRemoteRequest('uploadError', {
                message: ansi.red + err.message
            });
            this.sendRemoteRequest('peripheralUnplug', null);
        }

        this.tool = null;
    }

    async uploadFirmware (params) {
        if (this.wifi) {
            // Not a shortcoming of this transport: the ROM bootloader that
            // receives firmware runs before any firmware exists, so it has no
            // WiFi to be reached over. Only the cable can do this.
            this.sendRemoteRequest('setUploadAbortEnabled', false);
            this.sendRemoteRequest('uploadError', {
                message: `${ansi.red}Firmware can only be flashed over USB.\n` +
                    "The chip's built-in bootloader is what receives the firmware, and it can " +
                    'only be reached through the USB cable -- WiFi does not exist until the ' +
                    'firmware it would be replacing is already running.\n' +
                    'Connect the board with a USB cable, pick its COM port, and flash it there. ' +
                    'Programs can go back over WiFi afterwards.'
            });
            return;
        }

        switch (params.type) {
        case 'arduino':
            this.tool = new Arduino(this.peripheral.path, params, this.userDataPath,
                this.toolsPath, this.sendstd.bind(this));
            try {
                this.sendRemoteRequest('setUploadAbortEnabled', true);
                this.sendstd(`${ansi.clear}Disconnect serial port\n`);
                await this.disconnect();
                this.sendstd(`${ansi.clear}Disconnected successfully, flash program starting...\n`);
                const flashExitCode = await this.tool.flashRealtimeFirmware();
                await this.connect(this.peripheralParams, true);
                this.sendRemoteRequest('uploadSuccess', {aborted: flashExitCode === 'Aborted'});
            } catch (err) {
                this.sendRemoteRequest('uploadError', {
                    message: ansi.red + err.message
                });
            }
            break;
        case 'esp32MicroPython':
            this.tool = new Esp32MicroPython(this.peripheral.path, params, this.userDataPath,
                this.toolsPath, this.sendstd.bind(this), this.sendRemoteRequest.bind(this));
            try {
                this.sendRemoteRequest('setUploadAbortEnabled', true);
                this.sendstd(`${ansi.clear}Disconnect serial port\n`);
                await this.disconnect();
                this.sendstd(`${ansi.clear}Disconnected successfully, flashing MicroPython firmware...\n`);
                const flashExitCode = await this.tool.flashRealtimeFirmware();
                await this.connect(this.peripheralParams, true);
                this.sendRemoteRequest('uploadSuccess', {aborted: flashExitCode === 'Aborted'});
            } catch (err) {
                this.sendRemoteRequest('uploadError', {
                    message: ansi.red + err.message
                });
            }
            break;
        }

        this.tool = null;
    }

    async abortUpload () {
        if (this.tool !== null) {
            this.tool.abortUpload();
        }
    }

    sendstd (message) {
        if (this._socket) {
            this.sendRemoteRequest('uploadStdout', {
                message: message
            });
        }
    }

    dispose () {
        this.disconnect();
        this.stopWifiDiscovery();
        super.dispose();
        this.socket = null;
        this.wifi = null;
        this.peripheral = null;
        this.peripheralParams = null;
        this.services = null;
        this.reportedPeripherals = {};
        if (this.connectStateDetectorTimer) {
            clearInterval(this.connectStateDetectorTimer);
            this.connectStateDetectorTimer = null;
        }
        if (this.peripheralsScanorTimer) {
            clearInterval(this.peripheralsScanorTimer);
            this.peripheralsScanorTimer = null;
        }
    }
}

module.exports = SerialportSession;
