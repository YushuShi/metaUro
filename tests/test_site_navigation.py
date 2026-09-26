from pathlib import Path
from bs4 import BeautifulSoup
import app

ROOT = Path(__file__).resolve().parents[1]


def assert_shell(html):
    soup = BeautifulSoup(html, 'html.parser')
    headers = soup.select('header.site-header')
    assert len(headers) == 1
    nav = headers[0].select_one('nav.header-actions')
    controls = [node for node in nav.find_all(['a', 'button'])
                if not node.find_parent('dialog') and not node.find_parent(attrs={'role': 'menu'})]
    labels = [node.get('aria-label') or node.get_text(' ', strip=True) for node in controls]
    assert labels == ['Choose language', 'Settings', 'Study selection', 'Summary', 'About']
    assert headers[0].select_one('a.logo[data-site-link]')
    ids = [node['id'] for node in soup.select('[id]')]
    assert len(ids) == len(set(ids))
    assert not soup.select('.about-toolbar, .summary-back-link, .selection-kicker, .selection-unit-note')
    return soup


def test_all_public_views_use_same_navigation():
    app.app.config['TESTING'] = True
    client = app.app.test_client()
    for route in ['/study-selection', '/settings', '/about']:
        response = client.get(route)
        assert response.status_code == 200, route
        assert_shell(response.data)


def test_theme_and_settings_keep_navigation_accessible():
    css = (ROOT / 'static/site-navigation.css').read_text()
    assert 'background: var(--primary);' in css
    assert 'grid-template-columns: 36px repeat(4, minmax(0, 1fr))' in css
    assert 'scrollbar-gutter: stable' in css
    assert 'var(--site-header-height' in css


def test_source_pages_share_the_shell_without_rewriting_source_snapshots():
    minimal = '<html><head><title>Source</title></head><body><header></header><main><section class="hero"><h1>Title</h1><p>Old subtitle</p></section><a class="summary-back-link" href="/">Back</a><p class="summary-method-note">Old methods</p></main></body></html>'
    for source in ('MetaVir', 'metaPee'):
        with app.app.test_request_context('/evidence/prostate/?scope=prostate_cancer'):
            for page in ('', 'summary'):
                html = app.decorate_html(minimal, source, page)
                soup = assert_shell(html)
                assert not soup.select('.hero > p, .summary-method-note')
                assert soup.select_one('[data-uro-nav="summary"]')['href'].startswith(app.public_prefix(source))
    about = app.app.test_client().get('/about?scope=prostate_cancer')
    assert '/evidence/prostate/' in assert_shell(about.data).select_one('a.logo')['href']
