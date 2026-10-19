/**
 * The transparent desktop window — just the avatar, floating over your desktop.
 *
 * This is the SAME page as http://127.0.0.1:5173/, hosted in a window that has
 * a real alpha channel. That is the whole reason this file exists: Chromium can
 * paint a per-pixel transparent window, but no browser exposes it, so the page
 * has to be hosted rather than browsed.
 *
 * `?transparent=1` tells the page to drop its white background and hide its own
 * buttons (client/config.js -> TRANSPARENT_MODE). Nothing else changes: the
 * normal page at :5173 still works exactly as before, and this is an
 * alternative to it, not a replacement.
 *
 * HOW TO RUN IT
 *   1. start_servers.bat        (the stack must already be up)
 *   2. start_window.bat
 *
 * ctrl+shift+Q quits. ctrl+shift+D opens the console. There is no titlebar, so
 * drag the top 22 pixels of the window to move it.
 *
 * ONE CATCH: a `?scene=` preset that uses depth of field makes the canvas
 * opaque (the blur shader writes solid alpha), so a scene and this window are
 * mutually exclusive. Use one or the other.
 */

const { app, BrowserWindow, globalShortcut } = require('electron');

// --- Knobs ---------------------------------------------------------------
// 127.0.0.1, not localhost — see VRM_BASE_URL in server/config.py.
const VITE_URL = 'http://127.0.0.1:5173';
const WIDTH = 480;
const HEIGHT = 820;
const ALWAYS_ON_TOP = true;
// -------------------------------------------------------------------------

// The character is optional and normally left blank: with no `?character=`,
// the page asks the server which one it launched as, so the window and the
// server can never disagree. Pass one only to override that.
const CHARACTER = process.argv.slice(2).find((a) => !a.startsWith('-')) || '';

function createWindow() {
  const target = `${VITE_URL}/?transparent=1` + (CHARACTER ? `&character=${CHARACTER}` : '');
  console.log(`[shell] ${WIDTH}x${HEIGHT} alwaysOnTop=${ALWAYS_ON_TOP} character=${CHARACTER || "<ask the server>"}`);
  console.log(`[shell] -> ${target}`);

  const win = new BrowserWindow({
    width: WIDTH,
    height: HEIGHT,
    transparent: true,
    frame: false,
    hasShadow: false,            // Windows otherwise draws a shadow around the invisible frame
    backgroundColor: '#00000000',
    alwaysOnTop: ALWAYS_ON_TOP,
  });

  win.loadURL(target);
  win.webContents.on('did-finish-load', () => console.log('[shell] loaded'));
  win.webContents.on('did-fail-load', (_e, code, desc) =>
    console.error(`[shell] load failed (${code} ${desc}) — did you run start_servers.bat first?`));

  return win;
}

app.whenReady().then(() => {
  const win = createWindow();
  // Frameless means no titlebar and no close button, so these are the only
  // way out and the only way to see the page's console.
  globalShortcut.register('CommandOrControl+Shift+Q', () => app.quit());
  globalShortcut.register('CommandOrControl+Shift+D', () => win.webContents.toggleDevTools());
  console.log('[shell] ctrl+shift+Q quits · ctrl+shift+D devtools · drag the top 22px to move');
});

app.on('window-all-closed', () => app.quit());
