"""Site público (GitHub Pages) e segurança do repositório: espectador só leitura, admin restrito
pelo servidor, nenhum segredo publicado, mesma linguagem e build completo."""
import json
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
TX = "auth != null && root.child('access/transmitters').child(auth.uid).exists()"
ADMIN = "root.child('admins').child(auth.uid).val() === true"


def site_texts():
    for page in ('index.html', 'admin.html'):
        html = (SITE / page).read_text(encoding='utf-8')
        html = re.sub(r'<!--.*?-->', '', html, flags=re.S)
        html = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', html, flags=re.S)
        for m in re.finditer(r'>([^<>]+)<|(?:aria-label|title|placeholder|alt)="([^"]*)"', html):
            yield page, (m.group(1) or m.group(2) or '').strip()
    for name in ('acompanhar.js', 'sync-core.js', 'admin.js'):
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
                if rx.search(text):
                    problems.append(f'{name}: {rule}: {text[:90]}')
        self.assertEqual(problems, [], '\n'.join(problems))

    def test_spectator_is_read_only_and_has_no_credentials(self):
        # A chave de API web em config.js é pública; nada de senha, token ou escrita.
        sources = ''.join((SITE / f).read_text(encoding='utf-8')
                          for f in ('index.html', 'acompanhar.js', 'sync-core.js', 'config.js'))
        for forbidden in ('/api/trajectory', '/api/device', 'traj-start', 'password', 'Iniciar trajetória', 'refreshToken',
                          'serviceAccount', 'private_key', "method: 'POST'", 'method: "POST"', 'PATCH', 'PUT', 'DELETE'):
            self.assertNotIn(forbidden, sources, forbidden)
        for page in ('index.html', 'admin.html'):
            html = (SITE / page).read_text(encoding='utf-8')
            self.assertNotIn('unsafe-inline', html)
            self.assertNotIn('unsafe-eval', html)

    def test_admin_page_never_renders_request_data_as_html(self):
        js = (SITE / 'admin.js').read_text(encoding='utf-8')
        for unsafe in ('innerHTML', 'insertAdjacentHTML', 'outerHTML', 'localStorage', 'sessionStorage', 'document.cookie'):
            self.assertNotIn(unsafe, js, unsafe)          # token só em memória; dados só como texto

    def test_operator_controls_only_on_local_dashboard(self):
        page = (REPO / 'GATEWAY_PC' / 'dashboard.html').read_text(encoding='utf-8')
        for action in ('start', 'pause', 'resume', 'finish'):
            self.assertIn(f'data-action="{action}"', page)
        for login in ('id="sync-password"', 'id="sync-email"', '/api/sync/connect'):
            self.assertNotIn(login, page)                  # nenhum login do Firebase no computador da antena

    def test_rules_protect_writes(self):
        rules = json.loads((REPO / 'firebase' / 'database.rules.json').read_text(encoding='utf-8'))['rules']
        self.assertFalse(rules['.read'])
        self.assertFalse(rules['.write'])
        self.assertEqual(rules['live']['.write'], TX)
        self.assertEqual(rules['sessions']['$sid']['.write'], TX)
        self.assertNotIn('.read', rules['sessions'])                       # sem listagem de sessões
        self.assertFalse(rules['admins']['$uid']['.write'])                # papel de admin só pelo console
        self.assertIn(ADMIN, rules['access']['transmitters']['$uid']['.write'])
        self.assertNotIn('auth.uid === $uid', rules['access']['transmitters']['$uid']['.write'])
        self.assertIn('!data.exists()', rules['audit']['$k']['.write'])    # auditoria só de acréscimo
        self.assertIn("newData.child('uid').val() === auth.uid", rules['audit']['$k']['.validate'])
        for node in ('pairing', 'devices', 'audit'):
            self.assertIn(ADMIN, rules[node]['.read'])                    # listas só para admin
        self.assertIn('sessoes', rules)                                    # app de controle remoto preservado

    def test_no_private_credentials_in_repository(self):
        tracked = subprocess.run(['git', 'ls-files'], cwd=REPO, capture_output=True, text=True, check=True).stdout.split()
        secret = re.compile(r'BEGIN (RSA |EC )?PRIVATE KEY|"private_key"|AMf-[A-Za-z0-9_-]{40,}'
                            r'|"refreshToken"\s*:\s*"[A-Za-z0-9_-]{30,}')
        leaks = []
        for rel in tracked:
            path = REPO / rel
            if (path.suffix.lower() in ('.png', '.ico', '.woff', '.pdf', '.mp4', '.pyc') or not path.is_file()
                    or rel == 'GATEWAY_PC/tests/test_site.py'):   # este arquivo contém os próprios padrões
                continue
            if secret.search(path.read_text(encoding='utf-8', errors='ignore')):
                leaks.append(rel)
        self.assertEqual(leaks, [])
        joined = ' '.join(tracked)
        for name in ('transmissor.local.json', 'identidade-dispositivo.json', 'service-account'):
            self.assertNotIn(name, joined)

    def test_build_outputs_complete_site(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'site'
            subprocess.run([sys.executable, str(SITE / 'build.py'), str(out)], check=True, capture_output=True)
            for page in ('index.html', 'admin.html'):
                html = (out / page).read_text(encoding='utf-8')
                self.assertNotIn('__CONNECT_SRC__', html)
                self.assertNotIn('__AUTH_SRC__', html)
                self.assertIn('https://*.firebaseio.com', html)
                self.assertNotRegex(html, r'(?:href|src)="/')              # caminhos relativos
            self.assertIn('identitytoolkit.googleapis.com', (out / 'admin.html').read_text(encoding='utf-8'))
            self.assertNotIn('identitytoolkit', (out / 'index.html').read_text(encoding='utf-8'))
            for rel in ('dashboard.css', 'dashboard.js', 'sync-core.js', 'acompanhar.js', 'admin.js', 'config.js', '.nojekyll',
                        'assets/brand/logos/05-simbolo.svg', 'assets/brand/fonts/OpenSans-Regular.woff'):
                self.assertTrue((out / rel).is_file(), rel)
            self.assertNotIn('url("/assets', (out / 'dashboard.css').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
