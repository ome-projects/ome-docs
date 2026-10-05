"""Guard the runner toolchain prerequisite that pnpm v6's bootstrap needs."""

from pathlib import Path
import unittest

import yaml


class ToolchainTests(unittest.TestCase):
    def test_pnpm_bootstrap_has_modern_node_before_it_runs(self):
        root = Path(__file__).resolve().parents[2]
        checked = 0
        for path in (root / '.github/workflows').glob('*.yml'):
            workflow = yaml.safe_load(path.read_text())
            for name, job in workflow.get('jobs', {}).items():
                steps = job.get('steps', [])
                for index, step in enumerate(steps):
                    if not step.get('uses', '').startswith('pnpm/action-setup@'):
                        continue
                    checked += 1
                    with self.subTest(workflow=path.name, job=name):
                        nodes = [s for s in steps[:index]
                                 if s.get('uses', '').startswith('actions/setup-node@')]
                        self.assertTrue(nodes, 'Install Node before the pnpm bootstrap')
                        node = nodes[-1]
                        version = node['with'].get('node-version')
                        if version is None:
                            version = (root / node['with']['node-version-file']).read_text().strip()
                        self.assertEqual(str(version), '22')
                        self.assertIs(node['with'].get('check-latest'), True)
                        self.assertEqual(node.get('if'), step.get('if'))
                        self.assertNotEqual(node['with'].get('cache'), 'pnpm',
                                            'pnpm is not installed yet')
                        self.assertIs(node['with'].get('package-manager-cache'), False)
                        self.assertIs(step['with'].get('cache'), True)
        self.assertGreaterEqual(checked, 4)
