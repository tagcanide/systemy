"""Site público (GitHub Pages): só leitura, sem segredos, mesma linguagem e build completo."""
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_copy import RULES, STRING, strip_js_comments, EMOJI  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
SITE = REPO / 'site'


def site_texts():
    html = (SITE / 'index.html').read_text(encoding='utf-8')
    html = re.sub(r'<!--.*?-->', '', html, flags=re.S)
    html = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', html, flags=re.S)
    for m in re.finditer(r'>([^<>]+)<|(?:aria-label|title|placeholder|alt)="([^"]*)"', html):
        yield 'index.html', (m.group(1) or m.group(2) or '').strip()
    for name in ('acompanhar.js', 'sync-core.js'):
        js = strip_js_comments((SITE / name).read_text(encoding='utf-8'))
        for m in STRING.finditer(js):
            text = next(g for g in m.groups() if g is not None)
            if re.search(r'[A-Za-zÀ-ú]{2,}', text) or EMOJI.search(text):
                text = re.sub(r'\$\{[^}]*\}', '', text)
                yield name, re.sub(r'<[^>]+>', ' ', text).strip()


class SiteTests(unittest.TestCase):
    def test_site_text_follows_writing_rules(self):
        problems = []
        for name, text in site_texts():
            if not text or text == '—':
                continue
            for rule, rx in RULES.items():
                # O "!" de operadores JS não é texto; só strings com palavras chegam aqui.
                if rx.search(text):
                    problems.append(f'{name}: {rule}: {text[:90]}')
        self.assertEqual(problems, [], '\n'.join(problems))

    def test_spectator_has_no_transmitter_controls_or_secrets(self):
        sources = ''.join((SITE / f).read_text(encoding='utf-8')
                          for f in ('index.html', 'acompanhar.js', 'sync-core.js', 'config.js'))
        for forbidden in ('/api/trajectory', '/api/sync', 'traj-start', 'password', 'apiKey', 'Iniciar trajetória',
                          'serviceAccount', 'private_key', 'method: \'POST\'', 'method: "POST"', 'PATCH', 'PUT'):
            self.assertNotIn(forbidden, sources, forbidden)
        self.assertNotIn('unsafe-inline', sources)
        self.assertNotIn('unsafe-eval', sources)

    def test_operator_controls_only_on_local_dashboard(self):
        page = (REPO / 'GATEWAY_PC' / 'dashboard.html').read_text(encoding='utf-8')
        for action in ('start', 'pause', 'resume', 'finish'):
            self.assertIn(f'data-action="{action}"', page)

    def test_rules_protect_writes(self):
        import json
        rules = json.loads((REPO / 'firebase' / 'database.rules.json').read_text(encoding='utf-8'))['rules']
        self.assertFalse(rules['.read'])
        self.assertFalse(rules['.write'])
        writer = "auth != null && root.child('access/transmitters').child(auth.uid).val() === true"
        self.assertEqual(rules['live']['.write'], writer)
        self.assertEqual(rules['sessions']['$sid']['.write'], writer)
        self.assertNotIn('.read', rules['sessions'])            # sem listagem de sessões
        self.assertFalse(rules['access']['transmitters']['$uid']['.write'])

    def test_build_outputs_complete_site(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'site'
            subprocess.run([sys.executable, str(SITE / 'build.py'), str(out)], check=True, capture_output=True)
            html = (out / 'index.html').read_text(encoding='utf-8')
            self.assertNotIn('__CONNECT_SRC__', html)
            self.assertIn('https://*.firebaseio.com', html)
            for rel in ('dashboard.css', 'dashboard.js', 'sync-core.js', 'acompanhar.js', 'config.js', '.nojekyll',
                        'assets/brand/logos/05-simbolo.svg', 'assets/brand/fonts/OpenSans-Regular.woff'):
                self.assertTrue((out / rel).is_file(), rel)
            # Caminhos relativos: funciona no subcaminho do GitHub Pages.
            self.assertNotRegex(html, r'(?:href|src)="/')
            css = (out / 'dashboard.css').read_text(encoding='utf-8')
            self.assertNotIn('url("/assets', css)


if __name__ == '__main__':
    unittest.main()
