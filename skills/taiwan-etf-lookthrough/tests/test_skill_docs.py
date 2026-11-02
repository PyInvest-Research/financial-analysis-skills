"""Verify shipped guide contracts and execute its offline example."""

import json
from pathlib import Path
import re
import tempfile
import tomllib
import unittest
from unittest.mock import patch

import support  # noqa: F401
from lookthrough import __version__
from lookthrough.report import LIMITATIONS


ROOT = Path(__file__).resolve().parents[1]


class SkillDocsTests(unittest.TestCase):
    """Check actual package contents and runnable documentation."""

    def test_required_rules(self) -> None:
        """Preserve workflow, safety and fixed report limitations."""
        text = (ROOT / 'SKILL.md').read_text()
        for sentence in LIMITATIONS:
            self.assertEqual(text.count(sentence), 1)
        for rule in (
            '只限點官方匯出或讀完整展開表格',
            '遇登入、CAPTCHA、存取限制即停止並回報',
            '不讀取 cookie 或帳號資料，不操作其他分頁',
            '沒有瀏覽器工具時改用 Web fetch，再請使用者上傳官方檔',
            'result.json', 'ChatGPT Web', '固定示範特例', 'demo/README.md',
            'CHANGELOG.md', 'references/snapshot-coverage.md',
            '國外成分 ETF', '不得手抄權重', 'use_network=False',
        ):
            self.assertIn(rule, text)
        frontmatter = text.split('---', 2)[1]
        self.assertEqual(set(re.findall(r'^(\w+):', frontmatter, re.M)),
                         {'name', 'description'})

    def test_manifest(self) -> None:
        """Ship all runtime modules, metadata and essential references only."""
        manifest = json.loads((ROOT / 'skill.json').read_text())
        files = set(manifest['files'])
        expected = {str(p.relative_to(ROOT))
                    for p in (ROOT / 'scripts').rglob('*.py')}
        expected.update({
            'SKILL.md', 'CHANGELOG.md', 'agents/openai.yaml',
            'references/schema.md',
            'references/methodology.md', 'references/data-sources.md',
            'references/runtime-safety.md',
            'references/estimation-first.md',
            'references/official-fallback.md',
            'references/snapshot-coverage.md',
            'scripts/lookthrough/data/company-directory.json',
            'scripts/lookthrough/data/refresh-products.json',
            'scripts/lookthrough/data/refresh-candidates.json',
            'scripts/lookthrough/data/native-daily-products.json',
            'scripts/lookthrough/data/cathay-modern-lookup.json',
            'scripts/lookthrough/data/mixed-markets.json',
            'scripts/lookthrough/data/fubon-00903-markets.json',
            'scripts/lookthrough/data/fubon-00920-markets.json',
            'references/mixed-etfs.md',
            'references/research-and-annotations.md',
            'scripts/lookthrough/data/research-20261028.json',
            'scripts/lookthrough/data/research-publications.json',
            'scripts/lookthrough/data/research-snapshots-20261028.json',
            'LICENSE', 'skill.json', 'pyproject.toml', 'uv.lock',
            '.python-version',
        })
        expected.update({
            'demo/README.md', 'demo/portfolio.json',
            'demo/expected-result.json',
            'demo/report.md', 'demo/exposures.csv', 'demo/provenance.json',
            'demo/independent-2330-precheck.json',
            'demo/pre-run-evidence.json',
            'demo/generation-evidence.json',
            'demo/v1.3.0/expected-result.json',
            'demo/v1.3.0/report.md',
            'demo/v1.3.0/exposures.csv',
            'demo/v1.3.0/generation-evidence.json',
        })
        expected.update({f'demo/snapshots/{symbol}.json' for symbol in
                         ('0050', '006208', '0056', '00878', '00919')})
        self.assertEqual(files, expected)
        self.assertEqual(len(files), len(manifest['files']))
        for name in files:
            self.assertFalse(Path(name).is_absolute())
            self.assertNotIn('..', Path(name).parts)
            self.assertTrue((ROOT / name).is_file(), name)
        self.assertEqual(manifest['status'], 'published')
        self.assertEqual(manifest['version'], '1.6.1')
        self.assertEqual(manifest['version'], __version__)
        project = tomllib.loads((ROOT / 'pyproject.toml').read_text())
        self.assertEqual(project['project']['version'], __version__)
        lock = tomllib.loads((ROOT / 'uv.lock').read_text())
        locked = next(row for row in lock['package']
                      if row['name'] == 'taiwan-etf-lookthrough')
        self.assertEqual(locked['version'], __version__)
        for name in ('directory.py', 'publish.py'):
            self.assertIn(f'PyInvest-ETF-Lookthrough/{__version__}',
                          (ROOT / 'scripts/lookthrough' / name).read_text())
        self.assertIn('$taiwan-etf-lookthrough',
                      (ROOT / 'agents/openai.yaml').read_text())

    def test_documented_offline_example(self) -> None:
        """Run the exact documented Python block with network forbidden."""
        text = (ROOT / 'SKILL.md').read_text()
        code = re.search(r'```python\n(.*?)```', text, re.S).group(1)
        with tempfile.TemporaryDirectory() as folder:
            with patch('urllib.request.OpenerDirector.open',
                       side_effect=AssertionError('Network forbidden')):
                namespace = {'example_root': Path(folder)}
                exec(compile(code, 'SKILL.md', 'exec'), namespace)
            paths = namespace['outputs']
            self.assertEqual(set(paths),
                             {'result.json', 'report.md', 'exposures.csv'})
            result = json.loads(paths['result.json'].read_text())
            self.assertEqual(result['totals']['total_value'], 100000)
            self.assertEqual(result['totals']['coverage'], 1)
            self.assertEqual(result['limitations'], list(LIMITATIONS))
