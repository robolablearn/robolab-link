const dgram = require('dgram');
const os = require('os');

/**
 * Finding Mieo boards on the local network, and asking one to stop.
 *
 * Boards do not announce themselves unprompted -- they answer. Robolab
 * broadcasts a probe while the connection window is open and every board that
 * hears it replies with its name, which keeps the network quiet the rest of
 * the time and means a board that joins late is still found on the next probe.
 *
 * The stop message is the other half of mieowifi's UDP listener: it is the
 * only way to interrupt a program that is sitting in a forever loop, because
 * nothing services the WebREPL socket while user code is running.
 */

/**
 * Must match mieowifi.DISCOVERY_PORT on the board.
 *
 * Deliberately not 20112: that is the resource server's TCP port. UDP and TCP
 * ports are separate and would not actually have collided, but one number
 * meaning two things is how a confusing afternoon starts.
 */
const DISCOVERY_PORT = 20114;

const PROBE = Buffer.from('MIEO?');
const STOP = Buffer.from('MIEO!STOP');

/** How often to ask. Fast enough that a board turns up while the user is
 * still reading the connection window. */
const PROBE_INTERVAL_MS = 1000;

/** UDP can drop a packet without telling anyone, so a stop is sent more than
 * once. Three tries close together is plenty on a local network. */
const STOP_ATTEMPTS = 3;
const STOP_SPACING_MS = 60;

/**
 * Every broadcast address this machine can reach, one per network it is on.
 *
 * The all-ones address is included as well: some adapters take that and
 * nothing else, and sending to both costs one extra packet a second.
 * @returns {Array<string>} - addresses to send probes to.
 */
const broadcastAddresses = () => {
    const addresses = ['255.255.255.255'];
    const interfaces = os.networkInterfaces();

    Object.keys(interfaces).forEach(key => {
        (interfaces[key] || []).forEach(entry => {
            const isIpv4 = entry.family === 'IPv4' || entry.family === 4;
            if (!isIpv4 || entry.internal || !entry.netmask) return;

            const address = entry.address.split('.').map(Number);
            const netmask = entry.netmask.split('.').map(Number);
            if (address.length !== 4 || netmask.length !== 4) return;

            // Host bits all set: the directed broadcast for this subnet.
            const broadcast = address.map((octet, i) => (octet | (~netmask[i] & 0xFF))).join('.');
            if (!addresses.includes(broadcast)) {
                addresses.push(broadcast);
            }
        });
    });

    return addresses;
};

class MieoDiscovery {
    constructor () {
        this._socket = null;
        this._timer = null;
        this._onFound = null;
    }

    /**
     * Start probing. The callback is called once per reply, which means
     * repeatedly for the same board -- callers are expected to key on address.
     * @param {Function} onFound - called with {address, name} for each reply.
     */
    start (onFound) {
        if (this._socket) return;
        this._onFound = onFound;

        const socket = dgram.createSocket({type: 'udp4', reuseAddr: true});
        this._socket = socket;

        socket.on('error', () => {
            // No network, or the port is taken. Nothing to do but stop asking;
            // the serial port scan alongside this one still works.
            this.stop();
        });

        socket.on('message', (message, sender) => {
            const text = message.toString('utf8').trim();
            if (!text.startsWith('MIEO ')) return;
            // "MIEO <name> <address>". The address the packet actually came
            // from is more trustworthy than the one inside it, so use that.
            const name = text.split(' ')[1] || 'Mieo';
            if (this._onFound) {
                this._onFound({address: sender.address, name});
            }
        });

        socket.bind(() => {
            try {
                socket.setBroadcast(true);
            } catch (err) {
                // Some adapters refuse; the directed sends below may still work.
            }
            this._probe();
            this._timer = setInterval(() => this._probe(), PROBE_INTERVAL_MS);
        });
    }

    stop () {
        if (this._timer) {
            clearInterval(this._timer);
            this._timer = null;
        }
        if (this._socket) {
            const socket = this._socket;
            this._socket = null;
            socket.removeAllListeners();
            try {
                socket.close();
            } catch (err) {
                // Already closed.
            }
        }
        this._onFound = null;
    }

    _probe () {
        if (!this._socket) return;
        broadcastAddresses().forEach(address => {
            if (!this._socket) return;
            this._socket.send(PROBE, DISCOVERY_PORT, address, () => {
                // Unreachable networks are normal on a laptop with several
                // adapters; the reply from the one that works is what counts.
            });
        });
    }
}

/**
 * Ask a board to stop whatever it is running.
 *
 * Resolves once the messages have gone out -- there is no acknowledgement, and
 * waiting for one would mean the board answering while it is busy, which is
 * exactly the situation this exists to get out of.
 * @param {string} address - the board's address.
 * @returns {Promise} - resolves when the last message has been sent.
 */
const sendStop = address => new Promise(resolve => {
    const socket = dgram.createSocket('udp4');
    let sent = 0;

    const finish = () => {
        try {
            socket.close();
        } catch (err) {
            // Already closed.
        }
        resolve();
    };

    socket.on('error', finish);

    const sendOne = () => {
        socket.send(STOP, DISCOVERY_PORT, address, () => {
            sent += 1;
            if (sent >= STOP_ATTEMPTS) {
                return setTimeout(finish, STOP_SPACING_MS);
            }
            setTimeout(sendOne, STOP_SPACING_MS);
        });
    };

    sendOne();
});

module.exports = MieoDiscovery;
module.exports.sendStop = sendStop;
module.exports.DISCOVERY_PORT = DISCOVERY_PORT;
