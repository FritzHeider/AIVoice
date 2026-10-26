// Widget bundle size gate (specs/22 §4): dist/widget.js must stay at or below 60 KB gzipped.
import { readFileSync } from "node:fs";
import { gzipSync } from "node:zlib";

export const LIMIT_BYTES = 60 * 1024;

const bundle = new URL("../dist/widget.js", import.meta.url);
const gzipped = gzipSync(readFileSync(bundle), { level: 9 }).length;
const summary = `widget.js: ${gzipped} bytes gzip (limit ${LIMIT_BYTES})\n`;
if (gzipped > LIMIT_BYTES) {
  process.stderr.write(`size gate failed: ${summary}`);
  process.exit(1);
}
process.stdout.write(summary);
