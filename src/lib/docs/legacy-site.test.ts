import { describe, expect, it } from 'vitest';
import { legacySite } from './legacy-site';

const site = 'https://ome-project.org/docs';
const entries = [
	{ old: '_index.md', new: 'getting-started/index.md', rewrittenFrom: 'a1b2c3d4' },
	{ old: 'concepts/base_model.md', new: 'concepts/models/base-models.md', rewrittenFrom: null }
];

describe('legacySite', () => {
	it('forwards the root, the search page and each Hugo page', () => {
		const files = legacySite(entries, site);
		expect([...files.keys()]).toEqual([
			'index.html',
			'search/index.html',
			'404.html',
			'docs/index.html',
			'docs/concepts/base_model/index.html'
		]);
		expect(files.get('index.html')).toContain('content="0; url=https://ome-project.org/docs/"');
		expect(files.get('search/index.html')).toContain('url=https://ome-project.org/docs/search"');
		expect(files.get('docs/index.html')).toContain(
			'url=https://ome-project.org/docs/getting-started"'
		);
		expect(files.get('docs/concepts/base_model/index.html')).toContain(
			'<a href="https://ome-project.org/docs/concepts/models/base-models">'
		);
	});

	it('names the new site on the 404 page without forwarding', () => {
		const html = legacySite(entries, site).get('404.html');
		expect(html).toContain('href="https://ome-project.org/docs/search"');
		expect(html).not.toContain('http-equiv="refresh"');
	});
});
