import { pathToFileURL } from "node:url";
import path from "node:path";

const MOUNTS = { "/kit/": process.env.KIT_STATIC, "/static/": process.env.APP_STATIC };

export async function resolve(specifier, context, next) {
  for (const [prefix, dir] of Object.entries(MOUNTS)) {
    if (specifier.startsWith(prefix)) return next(pathToFileURL(path.join(dir, specifier.slice(prefix.length))).href, context);
  }
  return next(specifier, context);
}
