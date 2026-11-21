"""The first HTML response shows the same eligible estimates as the live chart."""
from html.parser import HTMLParser

from sample_size_prerender import render_default_panels


class Markup(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.text = []
        self.feed(html)

    def handle_starttag(self, tag, attributes):
        self.tags.append((tag, dict(attributes)))

    def handle_data(self, text):
        self.text.append(text)

    def elements(self, tag):
        return [attrs for name, attrs in self.tags if name == tag]


def test_saved_cards_escape_labels_and_apply_the_inclusive_cap():
    points = [
        dict(exposure='<script>alert("x")</script>', total_n=20000, study_count=2,
             effect_size=.8, dot_color='#abc', label_color='red;url(test)', rct_count=1, rct_marker='*'),
        dict(exposure="Over limit", total_n=20001, study_count=3, effect_size=.8),
        dict(exposure="One report", total_n=123, study_count=1, effect_size=.9),
        dict(exposure="Unmarked", total_n=456, study_count=2, effect_size=1.4),
    ]
    summary = dict(disease='unsafe" onclick="x', baseline_percent=2,
                   settings=dict(alpha=.05, power=.8, sidedness="two", allocation_ratio=1,
                                 control_exposure_percent=50), panels=[])
    for measurement in ("all", "dietary"):
        for design in ("prospective", "retrospective"):
            summary["panels"].append(dict(measurement=measurement, design=design,
                                          points=points if measurement == "all" else []))
    doc = Markup(render_default_panels(summary, 'disease<script>'))
    assert len(doc.elements('article')) == 4
    assert len(doc.elements('svg')) == 2
    assert len(doc.elements('circle')) == 4
    assert len(doc.elements('sup')) == 2
    assert all(tag['data-disease-key'] == summary['disease'] and 'onclick' not in tag
               for tag in doc.elements('svg'))
    assert not doc.elements('script')
    assert '<script>alert("x")</script>' in doc.text
    assert 'One report' not in doc.text and 'Over limit' not in doc.text
    assert 'Unmarked' in doc.text and '20,000' in doc.text
    assert 'ln(RR)' in doc.text and 'ln(OR)' in doc.text and '-0.223' in doc.text
    assert all('url(test)' not in tag['style'] for tag in doc.elements('g'))
    assert all('Maximum sample size 20,000' in tag['data-planning-context']
               for tag in doc.elements('svg'))


def test_continuous_zero_and_negative_effects_have_linear_coordinates():
    summary = dict(disease='cognition', baseline_percent=None,
                   settings=dict(alpha=.05, power=.8, sidedness='two', allocation_ratio=1,
                                 control_exposure_percent=50), panels=[])
    for measurement in ('all', 'dietary'):
        for design in ('prospective', 'retrospective'):
            points = [dict(exposure=f'Study {i}', effect_size=value, total_n=100, study_count=3)
                      for i, value in enumerate([-.25, 0])] if design == 'prospective' else []
            summary['panels'].append(dict(measurement=measurement, design=design, effect_label='MD',
                                          points=points, reason='Unavailable for mean differences.'))
    doc = Markup(render_default_panels(summary, 'Cognition'))
    assert len(doc.elements('svg')) == 2 and len(doc.elements('circle')) == 4
    assert 'MD' in doc.text and '-0.250' in doc.text and '0.000' in doc.text
    assert 'Unavailable for mean differences.' in doc.text
    assert not any(node.get('class') == 'sample-null-line' for node in doc.elements('line'))
