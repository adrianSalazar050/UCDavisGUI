// Minimal preload. The UI is a plain web app served by the local backend and
// needs no privileged bridge, so this deliberately exposes nothing. It exists
// so contextIsolation stays on with an explicit (empty) preload rather than
// none, and gives us a seam if a native bridge is ever needed.
"use strict";
