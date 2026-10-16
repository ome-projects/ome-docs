// The shape of redirects.json. This module imports neither the repository's
// table nor any JSON, so scripts/legacy-site.ts can run it under Node without
// a build.

/** One row of redirects.json, which maps each Hugo page to the page that replaces it. */
export interface RedirectEntry {
	/** Hugo page, relative to site/content/en/docs in ome-projects/ome, such as `concepts/base_model.md`. */
	old: string;
	/** Replacement, relative to src/lib/content, such as `concepts/models/base-models.md`. */
	new: string;
	/** Last commit that changed the old page when the new page was written; null while it is a draft. */
	rewrittenFrom: string | null;
}

const KEYS = new Set(['old', 'new', 'rewrittenFrom']);

/** Checks the shape of the redirects table. `checkRedirects` checks its contents. */
export function parseRedirects(value: unknown): RedirectEntry[] {
	if (!Array.isArray(value)) throw new Error('redirects.json must be an array');
	return value.map((entry: unknown, index) => {
		const where = `redirects.json[${index}]`;
		if (entry === null || typeof entry !== 'object' || Array.isArray(entry)) {
			throw new Error(`${where} must be an object`);
		}
		const record = entry as Record<string, unknown>;
		for (const key of Object.keys(record)) {
			if (!KEYS.has(key)) throw new Error(`${where} has unknown key "${key}"`);
		}
		const { old, new: replacement, rewrittenFrom } = record;
		if (typeof old !== 'string' || typeof replacement !== 'string') {
			throw new Error(`${where} needs string "old" and "new" paths`);
		}
		if (rewrittenFrom !== null && typeof rewrittenFrom !== 'string') {
			throw new Error(
				`${where} needs "rewrittenFrom": a commit SHA, or null while the page is a draft`
			);
		}
		return { old, new: replacement, rewrittenFrom };
	});
}

/** Path of a Hugo page under docs/: `concepts/_index.md` was served at `concepts/`, and `_index.md` at the root. */
export function legacyPath(old: string): string {
	return old.replace(/(?:^|\/)_index\.md$/, '').replace(/\.md$/, '');
}
