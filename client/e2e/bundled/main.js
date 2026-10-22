// A task as someone who installed the client from npm would write it, built by Vite
// (see build-bundled.js). The point is that Vite finds pig-worker.js and copies it
// into the build on its own, with nothing in the task saying where it is.
import * as pig from "@uwmadison-chm/pig-client";

window.pig = pig;
window.dispatchEvent(new Event("pig-ready"));
