const http = require('http');
const url = require('url');
const {Server} = require('ws');
const Emitter = require('events');
const path = require('path');
const fetch = require('node-fetch');
const clc = require('cli-color');

const mieoLibrary = require('./lib/mieo-library');

/**
 * Configuration the default user data path. Just for debug.
 * @readonly
 */
const DEFAULT_USER_DATA_PATH = path.join(__dirname, '../../.openblockData');

/**
 * Configuration the default tools path.
 * @readonly
 */
const DEFAULT_TOOLS_PATH = path.join(__dirname, '../tools');

/**
 * Configuration the default host.
 * @readonly
 */
const DEFAULT_HOST = '0.0.0.0';

/**
 * Configuration the default port.
 * @readonly
 */
const DEFAULT_PORT = 20111;

/**
 * Server name, ues in root path.
 * @readonly
 */
const SERVER_NAME = 'openblock-link-server';

/**
 * The time interval for retrying to open the port after the port is occupied by another openblock-resource server.
 * @readonly
 */
const REOPEN_INTERVAL = 1000 * 1;

/**
 * Configuration the server routers.
 * @readonly
 */
const ROUTERS = {
    '/openblock/serialport': require('./session/serialport') // eslint-disable-line global-require
};

/**
 * Where the board-side Python library is served from.
 *
 * Uploads over Bluetooth are driven from the editor, which cannot read this
 * package off disk itself. Rather than push fifty kilobytes through the
 * websocket on the off chance it is needed, the editor asks here: the bare
 * route lists what each file should be, and only the ones a board is actually
 * missing get fetched.
 * @readonly
 */
const LIBRARY_ROUTE = '/mieo/library';

/**
 * A server to provide local hardware api.
 */
class OpenBlockLink extends Emitter{
    /**
     * Construct a OpenBlock link server object.
     * @param {string} userDataPath - the path to save user data.
     * @param {string} toolsPath - the path of build and flash tools.
     */
    constructor (userDataPath, toolsPath) {
        super();

        if (userDataPath) {
            this.userDataPath = path.join(userDataPath, 'link');
        } else {
            this.userDataPath = path.join(DEFAULT_USER_DATA_PATH, 'link');
        }

        if (toolsPath) {
            this.toolsPath = toolsPath;
        } else {
            this.toolsPath = DEFAULT_TOOLS_PATH;
        }

        this._port = DEFAULT_PORT;
        this._host = DEFAULT_HOST;
        this._httpServer = http.createServer();
        this._socketServer = new Server({server: this._httpServer});

        this._socketServer.on('connection', (socket, request) => {
            const {pathname} = url.parse(request.url);
            const Session = ROUTERS[pathname];
            let session;
            if (Session) {
                session = new Session(socket, this.userDataPath, this.toolsPath);
                console.info('new connection');
                this.emit('new-connection');
            } else {
                return socket.close();
            }
            const dispose = () => {
                if (session) {
                    session.dispose();
                    session = null;
                }
            };
            socket.on('close', dispose);
            socket.on('error', dispose);
        })
            .on('error', e => {
                if (e.code !== 'EADDRINUSE') {
                    console.error(clc.red(`ERR!: ${e}`));
                }
            });
    }

    /**
     * Answer a request for the board-side library.
     *
     * `/mieo/library` gives the manifest -- what each file should be, by
     * checksum. `/mieo/library/<name>` gives one file, and only names in the
     * library are served, so this cannot be walked out of.
     * @param {http.IncomingMessage} request - the request being answered.
     * @param {http.ServerResponse} res - where to write the answer.
     */
    serveLibrary (request, res) {
        // The editor asks for this from a page loaded over file:// in the
        // packaged app, which the browser treats as a null origin -- so
        // without this the request is refused before it is ever sent. What is
        // served is a handful of read-only .py files on the loopback
        // interface, so there is nothing here worth guarding from a page that
        // could already reach it.
        res.setHeader('Access-Control-Allow-Origin', '*');

        const name = decodeURIComponent(request.url.slice(LIBRARY_ROUTE.length).replace(/^\//, ''));

        if (!name) {
            const body = JSON.stringify({files: mieoLibrary.manifest()});
            res.writeHead(200, {'Content-Type': 'application/json'});
            res.end(body);
            return;
        }

        if (!mieoLibrary.LIBRARY_FILES.includes(name)) {
            res.writeHead(404, {'Content-Type': 'text/plain'});
            res.end('not part of the Mieo library');
            return;
        }

        try {
            const contents = mieoLibrary.readLibrary(name);
            res.writeHead(200, {'Content-Type': 'text/x-python'});
            res.end(contents);
        } catch (err) {
            res.writeHead(500, {'Content-Type': 'text/plain'});
            res.end(err.message);
        }
    }

    isSameServer (host, port) {
        return new Promise((resolve, reject) => {
            fetch(`http://${host}:${port}`)
                .then(res => res.text())
                .then(text => {
                    if (text === SERVER_NAME) {
                        return resolve(true);
                    }
                    return resolve(false);
                })
                .catch(err => reject(err));
        });
    }

    /**
     * Start a server listening for connections.
     * @param {number} port - the port to listen.
     * @param {string} host - the host to listen.
     */
    listen (port, host) {
        if (port) {
            this._port = port;
        }
        if (host) {
            this._host = host;
        }

        this._httpServer.on('request', (request, res) => {
            if (request.url === '/') {
                res.writeHead(200, {'Content-Type': 'text/html'});
                res.end(SERVER_NAME);
                return;
            }
            if (request.url.startsWith(LIBRARY_ROUTE)) {
                this.serveLibrary(request, res);
                return;
            }
            // Anything else has to be answered too. Writing nothing leaves the
            // socket open until whoever asked gives up, which reads as "the
            // link server has hung" rather than "no such thing here".
            res.writeHead(404, {'Content-Type': 'text/plain'});
            res.end('not found');
        });

        this._httpServer.on('error', e => {
            this.isSameServer('127.0.0.1', this._port).then(isSame => {
                if (isSame) {
                    console.log(`Port is already used by other openblock-link server, will try reopening after ${REOPEN_INTERVAL} ms`); // eslint-disable-line max-len
                    setTimeout(() => {
                        this._httpServer.close();
                        this._httpServer.listen(this._port, this._host);
                    }, REOPEN_INTERVAL);
                    this.emit('port-in-use');
                } else {
                    const info = `ERR!: error while trying to listen port ${this._port}: ${e}`;
                    console.error(clc.red(info));
                    this.emit('error', info);
                }
            });
        });

        this._httpServer.listen(this._port, '0.0.0.0', () => {
            this.emit('ready');
            console.info(clc.green(`Openblock link server start successfully, socket listen on: http://${this._host}:${this._port}`));
        });
    }
}

module.exports = OpenBlockLink;
