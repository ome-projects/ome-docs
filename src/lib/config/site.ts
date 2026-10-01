/**
 * Site identity and repository locations. `repo` is the product: the GitHub
 * badge, the latest release and the issue links read it. `docsRepo` is this
 * repository: the Edit and source links read it, with `branch` and
 * `contentDir`.
 */
export const site = {
	name: 'OME',
	longName: 'Open Model Engine',
	description:
		'OME is a Kubernetes operator for serving large language models. Declare a model, and OME picks the runtime, places it on the right GPUs, and lets you roll out changes as canaries.',
	url: 'https://lightseek.org/ome',
	repo: 'ome-projects/ome',
	docsRepo: 'ome-projects/ome-docs',
	branch: 'main',
	contentDir: 'src/lib/content',
	legacyDocsUrl: 'https://ome-projects.github.io/ome/docs'
} as const;

export const repoUrl = `https://github.com/${site.repo}`;
export const docsRepoUrl = `https://github.com/${site.docsRepo}`;

/** GitHub editor URL for a content path such as `guides/index.md`. */
export function editUrl(path: string): string {
	return `${docsRepoUrl}/edit/${site.branch}/${site.contentDir}/${path}`;
}

/** Raw Markdown URL for a content path. */
export function sourceUrl(path: string): string {
	return `https://raw.githubusercontent.com/${site.docsRepo}/${site.branch}/${site.contentDir}/${path}`;
}
