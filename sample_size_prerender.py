"""Render saved sample-size panel data in the first HTML response.

The release builder uses the same label layout, bounds and public units as
static/sample-size-summary.js. No pooling or sample-size calculation runs here.
"""
import math
from decimal import Decimal, ROUND_HALF_UP, localcontext
import re
import textwrap

from jinja2 import Environment, StrictUndefined

_COLOR = re.compile(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3}(?:[0-9a-fA-F]{2})?)?\Z")
_PANEL_ORDER = [(m, d) for m in ("all", "dietary") for d in ("prospective", "retrospective")]
_TEMPLATE = Environment(autoescape=True, undefined=StrictUndefined, trim_blocks=True,
                        lstrip_blocks=True).from_string("""
{% for panel in panels %}
<article class="sample-plot-card">
<h2>{{ panel.title }}</h2>
<p class="sample-panel-note">{{ panel.count }} exposures · {{ panel.design_label }} planning · Maximum N = {{ maximum_label }}{% if panel.above %} · {{ panel.above }} above maximum{% endif %}</p>
{% if panel.note %}<p class="sample-panel-note">{{ panel.note }}</p>{% endif %}
{% if panel.count %}
<div class="sample-chart-scroll">
<svg viewBox="0 0 960 {{ panel.height }}" class="sample-chart" role="img" aria-label="{{ panel.aria }}" data-disease="{{ label }}" data-disease-key="{{ disease }}" data-planning-context="{{ context }}" data-maximum-sample-size="{{ maximum }}">
{% for tick in panel.ticks %}
<line x1="82" y1="{{ tick.y }}" x2="650" y2="{{ tick.y }}" stroke="#e3e9ee"></line>
<text x="70" y="{{ tick.y + 4 }}" text-anchor="end" class="sample-tick">{{ tick.y_label }}</text>
<text x="{{ tick.x }}" y="{{ panel.height - 50 }}" text-anchor="middle" class="sample-tick">{{ tick.x_label }}</text>
{% endfor %}
<path d="M82,40V{{ panel.height - 75 }}H650" fill="none" stroke="#81909b"></path>
{% if panel.ratio %}<line x1="{{ panel.null_x }}" y1="40" x2="{{ panel.null_x }}" y2="{{ panel.height - 75 }}" stroke="#81909b" stroke-dasharray="5 4" class="sample-null-line"></line>{% endif %}
<text x="366" y="{{ panel.height - 20 }}" text-anchor="middle" class="sample-axis">{{ panel.axis }}</text>
<text transform="translate(22 {{ (panel.height - 35) / 2 }}) rotate(-90)" text-anchor="middle" class="sample-axis">log₁₀(required total sample size)</text>
{% for row in panel.chart_rows %}
<g tabindex="0" style="--sample-dot-color:{{ row.dot_color }};--sample-label-color:{{ row.label_color }}">
<title>{{ row.exposure }}: {{ panel.measure }} {{ row.raw_display }}{% if panel.ratio %}; {{ panel.axis }} {{ row.display }}{% endif %}; {{ row.n_label }} total participants; {{ row.study_label }} studies. {{ row.trial_note }}</title>
<circle cx="{{ row.x }}" cy="{{ row.y }}" r="5" fill="{{ row.dot_color }}"></circle>
<text x="{{ row.label_x }}" y="{{ row.label_y }}" class="sample-point-label" fill="{{ row.label_color }}">{% for line in row.lines %}<tspan x="{{ row.label_x }}" dy="{{ 0 if loop.first else 15 }}">{{ line }}{% if loop.revindex == 2 and row.marker %}<tspan baseline-shift="super" font-size="9px" class="sample-rct-marker">{{ row.marker }}</tspan>{% endif %}</tspan>{% endfor %}</text>
</g>
{% endfor %}
</svg></div>
<div class="sample-panel-controls"><a class="sample-plot-link" href="#">View full-size plot</a><a class="sample-plot-link" href="#">Download full-size SVG</a></div>
<details><summary>View exposures, sample sizes and study counts</summary><div class="sample-table-scroll"><table>
<thead><tr><th scope="col">Rank</th>
{% for field, heading in panel.headings %}
<th scope="col" aria-sort="{{ 'ascending' if field == 'total_n' else 'none' }}"><button class="sample-sort-button" type="button" data-sort-field="{{ field }}" aria-label="Sort by {{ heading }}, {{ 'descending' if field == 'total_n' else 'ascending' }}">{{ heading }}<span class="sample-sort-indicator" aria-hidden="true">{{ '▲' if field == 'total_n' else '▲▼' }}</span></button></th>
{% endfor %}
</tr></thead><tbody>
{% for row in panel.table_rows %}
<tr><td>{{ loop.index }}</td><td style="color:{{ row.label_color }}">{{ row.exposure }}{% if row.marker %}<sup class="sample-rct-marker" title="{{ row.trial_note }}">{{ row.marker }}</sup>{% endif %}</td><td>{{ row.display }}</td><td>{{ row.n_label }}</td><td>{{ row.study_label }}</td></tr>
{% endfor %}
</tbody></table></div></details>
{% else %}
<p>{{ panel.empty_message }}</p>
{% endif %}
{% if panel.omitted %}
<details><summary>Unavailable sample sizes ({{ panel.omitted|length }})</summary>
<ul>{% for omission in panel.omitted %}<li>{{ omission.exposure }}: {{ omission.reason }}</li>{% endfor %}</ul></details>
{% endif %}
</article>
{% endfor %}
""")


def _number(value):
    try:
        return float(value)
    except (ValueError, TypeError, OverflowError):
        return math.nan


def _color(value, fallback):
    return value if isinstance(value, str) and _COLOR.fullmatch(value) else fallback


def _effect(value):
    # Match the browser's display precision before rounding half away from zero.
    # libm and JavaScript Math.log can differ by one ulp at a rounding boundary.
    with localcontext() as context:
        context.rounding = ROUND_HALF_UP
        return format(Decimal(format(value, ".15g")), ".3f")


def _rows(points, ratio):
    for point in points:
        effect, n, studies = (_number(point.get(k)) for k in ("effect_size", "total_n", "study_count"))
        if (not all(map(math.isfinite, (effect, n, studies))) or studies < 2
                or n <= 0 or ratio and effect <= 0):
            continue
        scaled = math.log(effect) if ratio else effect
        exposure = str(point["exposure"])
        dot = _color(point.get("dot_color") or point.get("color"), "#2475c0")
        marker = point.get("rct_marker", "")
        marker = marker if marker in ("*", "#") else ""
        note = ""
        if marker:
            direction = "all estimates share" if marker == "*" else "at least one estimate does not share"
            note = f'{point["rct_count"]:,} randomized trial report(s): {direction} the pooled effect direction.'
        yield dict(exposure=exposure, n=n, n_label=f"{n:,.0f}", study_label=f"{studies:,.0f}",
                   scaled=scaled, raw_display=_effect(effect), display=_effect(scaled),
                   marker=marker, trial_note=note, dot_color=dot,
                   label_color=_color(point.get("label_color") or point.get("color"), dot))


def _layout(panel, rows, maximum):
    height = max(540, 130 + sum((math.ceil(len(row["exposure"]) / 30) + 1) * 15 + 16 for row in rows))
    values = [row["scaled"] for row in rows]
    if panel["ratio"]:
        values.append(0)
    xmin, xmax = min(values), max(values)
    xpad = max((xmax - xmin) * .12, abs(xmin) * .04, .03)
    xmin, xmax = xmin - xpad, xmax + xpad
    ymin, ymax = math.log10(min(row["n"] for row in rows)), math.log10(maximum)
    ymin -= max((ymax - ymin) * .08, .15)

    def x(value):
        return round(82 + (value - xmin) / (xmax - xmin) * 568, 5)

    def y(value):
        return round(40 + (ymax - value) / (ymax - ymin) * (height - 115), 5)

    ticks = []
    for i in range(6):
        xv, yv = xmin + (xmax - xmin) * i / 5, ymin + (ymax - ymin) * i / 5
        ticks.append(dict(x=x(xv), y=y(yv), x_label=_effect(xv), y_label=f"{yv:.2f}"))
    for row in rows:
        lines = textwrap.wrap(" ".join(row["exposure"].split()), 34, break_long_words=False,
                              break_on_hyphens=False) + [f'N = {row["n_label"]}']
        px, py = x(row["scaled"]), y(math.log10(row["n"]))
        row.update(x=px, y=py, lines=lines, label_x=round(px + 13, 5),
                   label_y=round(py - (len(lines) * 15 + 4) / 2 + 12, 5))
    panel.update(height=height, ticks=ticks, null_x=x(0) if panel["ratio"] else None,
                 chart_rows=sorted(rows, key=lambda row: row["y"]),
                 table_rows=sorted(rows, key=lambda row: (row["n"], [
                     int(s) if s.isdigit() else s.casefold() for s in re.split(r"(\d+)", row["exposure"])
                 ])))


def default_planning_context(summary, maximum=20000):
    settings = summary["settings"]
    baseline = _number(summary.get("baseline_percent"))
    context = [f"Baseline incidence {baseline:.6g}%" if math.isfinite(baseline)
               else "Baseline incidence unavailable",
               f'{settings["control_exposure_percent"]:g}% exposure among controls',
               f'allocation 1:{settings["allocation_ratio"]:g}',
               f'{settings["power"] * 100:g}% power', f'α = {settings["alpha"]:g}',
               "one-sided" if settings["sidedness"] == "one" else "two-sided",
               f"Maximum sample size {maximum:,}", summary.get("note"), *(summary.get("notices") or [])]
    return " · ".join(str(item) for item in context if item)


def render_default_panels(summary, disease_label, maximum=20000):
    """HTML for the saved default plot grid. Jinja escapes every data field."""
    panels = []
    lookup = {(p["measurement"], p["design"]): p for p in summary["panels"]}
    for measurement, design in _PANEL_ORDER:
        data = lookup[measurement, design]
        measure = data.get("effect_label") or data.get("effect_measure") or ("RR" if design == "prospective" else "OR")
        ratio = measure.upper() in ("RR", "OR")
        axis = f"ln({measure})" if ratio else measure
        group = "Dietary only" if measurement == "dietary" else "All exposure measurements"
        eligible = list(_rows(data.get("points", []), ratio))
        visible = [row for row in eligible if row["n"] <= maximum]
        above = len(eligible) - len(visible)
        panel = dict(title=f"{group} · {design.capitalize()}", measure=measure, axis=axis,
                     aria=f"{group}, {design}: {axis} versus log base 10 of required total sample size.",
                     ratio=ratio, count=len(visible), above=above, note=data.get("note"),
                     design_label="Unmatched case-control" if design == "retrospective" else "Prospective",
                     omitted=data.get("omitted", []),
                     headings=[("exposure", "Exposure"), ("effect_size", f"Effect size {axis}" if ratio else "Effect size"),
                               ("total_n", "Total N"), ("study_count", "Studies")])
        if visible:
            _layout(panel, visible, maximum)
        elif above:
            panel["empty_message"] = f"No exposures require {maximum:,} or fewer participants. Increase the maximum sample size to show more exposures."
        else:
            panel["empty_message"] = data.get("reason") or "No exposures have at least two eligible studies and a finite required sample size."
        panels.append(panel)
    return _TEMPLATE.render(panels=panels, maximum=maximum, maximum_label=f"{maximum:,}",
                            disease=summary["disease"], label=disease_label,
                            context=default_planning_context(summary, maximum))
