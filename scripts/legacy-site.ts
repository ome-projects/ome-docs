// Writes the site that replaces the Hugo site at its old address,
// ome-projects.github.io/ome: a page per Hugo page that forwards to the page
// that replaced it, from redirects.json. The Pages workflow of ome-projects/ome
// runs it on a checkout of this repository. It needs only Node:
//
//   node --experimental-strip-types scripts/legacy-site.ts <out-dir>
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { site } from '../src/lib/config/site.ts';
import { legacySite } from '../src/lib/docs/legacy-site.ts';
import { parseRedirects } from '../src/lib/docs/redirect-table.ts';

const out = process.argv[2];
if (!out) {
	console.error('usage: node --experimental-strip-types scripts/legacy-site.ts <out-dir>');
	process.exit(1);
}

const root = fileURLToPath(new URL('..', import.meta.url));
const entries = parseRedirects(JSON.parse(readFileSync(join(root, 'redirects.json'), 'utf8')));
const files = legacySite(entries, site.url);
for (const [path, html] of files) {
	const file = join(out, path);
	mkdirSync(dirname(file), { recursive: true });
	writeFileSync(file, html);
}
console.log(`Wrote ${files.size} pages to ${out}`);
