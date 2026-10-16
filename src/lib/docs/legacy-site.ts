import { pathToRoute } from './paths.ts';
import { legacyPath, type RedirectEntry } from './redirect-table.ts';

// The site that replaces the Hugo site at its old address: a page per Hugo
// page that forwards to the page that replaced it. Node runs this module
// without a build, so it stays free of SvelteKit and JSON imports.

function page(title: string, head: string, body: string): string {
	return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>${title}</title>
${head}
</head>
<body>
${body}
</body>
</html>
`;
}

/** A page that forwards to `url` and names it, for readers whose browser doesn't follow. */
export function forwardingPage(url: string): string {
	return page(
		'Moved',
		`<link rel="canonical" href="${url}">
<meta http-equiv="refresh" content="0; url=${url}">`,
		`<p>This page moved to <a href="${url}">${url}</a>.</p>`
	);
}

/**
 * The files of the old site, keyed by path under its root, such as
 * `docs/concepts/base_model/index.html`: the root and the search page forward
 * to their counterparts, each Hugo page forwards to its replacement, and
 * 404.html names the new site for every other address.
 */
export function legacySite(
	entries: readonly RedirectEntry[],
	siteUrl: string
): Map<string, string> {
	const files = new Map<string, string>();
	files.set('index.html', forwardingPage(`${siteUrl}/`));
	files.set('search/index.html', forwardingPage(`${siteUrl}/search`));
	files.set(
		'404.html',
		page(
			'Page not found',
			'',
			`<h1>Page not found</h1>
<p>The OME documentation moved to <a href="${siteUrl}/">${siteUrl}/</a>. <a href="${siteUrl}/search">Search</a> it for this page.</p>`
		)
	);
	for (const entry of entries) {
		const stem = legacyPath(entry.old);
		files.set(
			`docs/${stem ? `${stem}/` : ''}index.html`,
			forwardingPage(`${siteUrl}/${pathToRoute(entry.new)}`)
		);
	}
	return files;
}
