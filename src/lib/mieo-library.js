const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

/**
 * The board-side library, and the bookkeeping that tells whether a board
 * already has the current copy of it.
 *
 * Both uploaders read this. Over USB it hardly matters -- copying fifty
 * kilobytes down the cable is a second's work -- but over WiFi it is the
 * difference between an upload that feels instant and one that does not, so
 * the manifest is written by both and every board carries the same record.
 */

/** Where the .py files that go on the board live in this package. */
const LIBRARY_DIR = path.join(__dirname, '../upload');

/**
 * What gets copied to the board, in the order it is copied.
 *
 * display.py and sound.py before mieo.py because mieo imports both, and
 * boot.py last because
 * it is what runs both mieoupload and mieoble: a board interrupted mid-upload
 * is then left pointing at a library it actually has.
 * @readonly
 */
const LIBRARY_FILES = ['display.py', 'sound.py', 'pins.py', 'motors.py',
    'linefollower.py', 'mieo.py', 'mieoble.py', 'mieoupload.py', 'boot.py'];

/** Where the board records the checksum of each library file it holds. */
const VERSION_FILE = 'libver.json';

const checksum = buffer => crypto.createHash('md5').update(buffer).digest('hex');

/**
 * @param {string} name - one of LIBRARY_FILES.
 * @returns {string} - the full path to it in this package.
 */
const libraryPath = name => path.join(LIBRARY_DIR, name);

/**
 * @param {string} name - one of LIBRARY_FILES.
 * @returns {Buffer} - its contents.
 */
const readLibrary = name => fs.readFileSync(libraryPath(name));

/**
 * What a board should be holding, as filename to checksum.
 * @returns {object} - the manifest to write to the board.
 */
const manifest = () => {
    const result = {};
    LIBRARY_FILES.forEach(name => {
        result[name] = checksum(readLibrary(name));
    });
    return result;
};

module.exports = {
    LIBRARY_DIR,
    LIBRARY_FILES,
    VERSION_FILE,
    checksum,
    libraryPath,
    readLibrary,
    manifest
};
