import table from '../../../redirects.json';
import { site } from '../config/site';
import { parseRedirects, type RedirectEntry } from './redirect-table.ts';

export { legacyPath, parseRedirects, type RedirectEntry } from './redirect-table.ts';

export const redirects: readonly RedirectEntry[] = parseRedirects(table);

/** Hugo pages that a new page replaces, in table order. */
export function replacedPages(entries: readonly RedirectEntry[], path: string): string[] {
	return entries.filter((entry) => entry.new === path).map((entry) => entry.old);
}

/** Where a Hugo page's source is kept: its old address now forwards to the page that replaced it. */
export function legacyUrl(old: string): string {
	return `https://github.com/${site.repo}/blob/main/site/content/en/docs/${old}`;
}
