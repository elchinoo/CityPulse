// Lets node load the dashboard's browser modules: "/kit/..." and "/static/..." are served by the app, not by a file system root.
import { register } from "node:module";

register(new URL("./hooks.mjs", import.meta.url));
