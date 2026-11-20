"""Live formatting for the two native engines, separate from sealed outputs."""
import base64
from functools import partial
import io
import math


def install(backend):
    """Install the live plot renderers inside the isolated HTTP worker."""
    backend._forest_plots = partial(_forest_plots, backend)
    backend._diagnostic_plots = partial(_diagnostic_plots, backend)


def _forest_plots(backend, studies, canonical, context):
    """One logarithmic forest, with fixed three-decimal CI columns."""
    from matplotlib.figure import Figure
    from matplotlib.ticker import FuncFormatter, LogLocator, NullFormatter
    import textwrap
    target, choice = context['settings']['effect_measure'], context['settings']['forest_scale']
    rows = backend.forest_annotation_rows(studies, canonical, context)
    if not rows:
        return {}
    label_scales = ['original', 'log'] if choice == 'both' else [choice]
    # Keep numerical columns unchanged; transfer 1.7 units from Study to the
    # graphical axis, making its 3.4-unit width 1.5 times wider.
    figure = Figure(figsize=(13.5 if choice == 'both' else 11.7, max(3.4, 2 + 0.36 * len(rows))), layout='constrained')
    grid = figure.add_gridspec(1, 2 + len(label_scales), width_ratios=[4.2, 5.1] + [1.8] * len(label_scales))
    names = figure.add_subplot(grid[0, 0])
    axis = figure.add_subplot(grid[0, 1], sharey=names)
    columns = [figure.add_subplot(grid[0, 2 + i], sharey=names) for i in range(len(label_scales))]
    names.set_ylim(len(rows) - 0.4, -1.1)
    names.set_xlim(0, 1)
    names.axis('off')
    names.text(0, -0.8, 'Study', weight='bold', fontsize=9)
    for y, row in enumerate(rows):
        pooled = row['study'] == 'Pooled result'
        weight = 'bold' if pooled else 'normal'
        names.text(0, y, textwrap.fill(row['study'], 54, break_long_words=False), va='center', fontsize=8, weight=weight, **({'color':'#000080'} if row.get('preprint_derived') else {}))
        point, low, high = row['estimate'], row['ci_low'], row['ci_upp']
        axis.errorbar(point, y, xerr=[[point - low], [high - point]], fmt='D' if pooled else 'o',
                      color='#1e3a5f' if pooled else '#377d97', capsize=3)
    axis.tick_params(axis='y', left=False, labelleft=False)
    axis.set_xscale('log')
    lower, upper = axis.get_xlim()
    close_range = math.log10(upper / lower) <= 2
    axis.xaxis.set_major_locator(LogLocator(base=10, subs=(1, 2, 5) if close_range else (1,), numticks=7))
    axis.xaxis.set_major_formatter(FuncFormatter(lambda value, position: f'{value:.3f}'))
    axis.xaxis.set_minor_formatter(NullFormatter())
    axis.tick_params(axis='x', labelsize=8)
    axis.axvline(1, color='#777777', linestyle='--', linewidth=1)
    axis.set_xlabel(f'{target} (logarithmic axis)')
    for column, scale in zip(columns, label_scales):
        column.axis('off')
        column.set_xlim(0, 1)
        column.text(0.03, -0.8, f'{"ln(" + target + ")" if scale == "log" else target} (95% CI)', weight='bold', fontsize=9)
        for y, row in enumerate(rows):
            prefix = 'log_' if scale == 'log' else ''
            values = [row[prefix + key] for key in ('estimate', 'ci_low', 'ci_upp')]
            formatted = [f'{v:.3f}' for v in values]
            column.text(0.03, y, f'{formatted[0]} ({formatted[1]}, {formatted[2]})', va='center',
                        fontsize=8, weight='bold' if row['study'] == 'Pooled result' else 'normal')
    figure.suptitle(f'{context["disease"]} — {target}', fontsize=12)
    buffer = io.BytesIO()
    figure.savefig(buffer, format='png', dpi=140)
    value = 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii')
    figure.clear()
    return {'log': value}


def _diagnostic_plots(backend, context, canonical):
    """Render fresh diagnostics on the same canonical log-RR observations."""
    from matplotlib.figure import Figure
    rows = []
    for row in context.get('pooled_plot_rows', []):
        effect, error = backend._number(row.get('log_ES')), backend._number(row.get('log_SE'))
        if effect is not None and error is not None and error > 0:
            rows.append((effect, error, row))
    if len(rows) < 2:
        return {}
    labels = backend._distinct_study_labels([row[2] for row in rows], compact=True)
    def encode(figure):
        buffer = io.BytesIO()
        figure.savefig(buffer, format='png', dpi=140)
        figure.clear()
        return 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii')
    figure = Figure(figsize=(8, 5.5), layout='constrained')
    axis = figure.subplots()
    axis.scatter([r[0] for r in rows], [r[1] for r in rows], color='#377d97')
    pooled = backend._number(canonical.get('pooled_es'))
    if pooled is not None and pooled > 0:
        center = math.log(pooled)
        maximum = max(r[1] for r in rows) * 1.1
        axis.plot([center - 1.96 * maximum, center, center + 1.96 * maximum],
                  [maximum, 0, maximum], linestyle='--', color='#777777')
        axis.axvline(center, color='#dc2626', linestyle='--', linewidth=1.5,
                     label=f'Pooled RR = {pooled:.3f}')
        axis.legend(loc='best', fontsize=8, frameon=False)
    axis.invert_yaxis()
    axis.set(xlabel='ln(RR)', ylabel='Standard error of ln(RR)',
             title=f'Funnel Plot: {context.get("disease", "")} vs {context.get("exposure", "")}')
    plots = {'funnel': encode(figure)}
    if len(rows) >= 3:
        weights = [1 / r[1] ** 2 for r in rows]
        total = sum(weights)
        mean = sum(w * r[0] for w, r in zip(weights, rows)) / total
        contributions = [w * (r[0] - mean) ** 2 for w, r in zip(weights, rows)]
        influences = []
        for weight, row in zip(weights, rows):
            omitted_mean = (total * mean - weight * row[0]) / (total - weight)
            influences.append(abs(omitted_mean - mean) * math.sqrt(total))
        # Keep each author/year/PMID directly beside its dot. Overlap is
        # intentional; no leader lines or detached label columns are used.
        figure = Figure(figsize=(8, 5.5), layout='constrained')
        axis = figure.subplots()
        axis.scatter(contributions, influences, color='#377d97', zorder=3)
        axis.margins(x=0.15, y=0.15)
        for label, contribution, influence in zip(labels, contributions, influences):
            axis.annotate(label, (contribution, influence), xytext=(4, 4),
                          textcoords='offset points', fontsize=7.5, va='bottom',
                          ha='left', annotation_clip=False)
        axis.set(xlabel='Contribution to Cochran Q', ylabel='Standardized change in fixed-effect log(RR)')
        figure.suptitle('Baujat diagnostic on canonical RR', fontsize=12)
        plots['baujat'] = encode(figure)
    return plots
