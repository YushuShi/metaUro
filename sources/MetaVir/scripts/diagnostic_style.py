"""Shared Plot.R diagnostic style: square pages, 14 pt bold labels, log10 p.

Scientific eligibility stays with each repository's caller. This module changes
only presentation, and preserves the original point coordinates except for the
explicitly requested logarithm base of the Egger axis.
"""
import colorsys
import hashlib
import math
import re
import textwrap
from pathlib import Path
import numpy as np
import matplotlib.pyplot as plt
from matplotlib import patheffects

STYLE_VERSION = 'square-log10-large-close-2026-09-17'

def wrap_label(value, width=95):
    width = max(12, width // 2) if re.search(r'[\u3000-\u9fff\uac00-\ud7af]', str(value)) else width
    return '\n'.join(textwrap.fill(line, width=width, break_long_words=True, break_on_hyphens=False) for line in str(value).splitlines())

def exposure_color(exposure):
    key = re.sub(r'[^a-z0-9]+', '_', str(exposure).lower()).strip('_')
    hue = int.from_bytes(hashlib.sha256(key.encode()).digest()[:4], 'big') / 2**32
    return colorsys.hls_to_rgb(hue, 0.43, 0.65)

def _adjacent_exposure_labels(fig, ax, labels):
    """Keep every label near its marker while avoiding labels and other dots.

    Search label edges, rather than text centers, so a long exposure name can
    sit directly beside its point. All candidates have a bounded edge gap;
    the caller can enlarge a crowded page instead of moving a name far away.
    """
    if not labels:
        return
    for label in labels:
        label['text'] = wrap_label(label['text'], 30)
        if 'annotation' not in label:
            label['annotation'] = ax.annotate(label['text'], (label['x'], label['y']),
                xytext=(0, 0), textcoords='offset points', ha='center', va='center',
                color=label['color'], zorder=5)
        annotation = label['annotation']
        annotation.set_position((0, 0))
        annotation.set_text(label['text'])
        annotation.set_fontsize(14)
        annotation.set_fontweight('bold')
        annotation.set_ha('center')
        annotation.set_va('center')
        annotation.set_path_effects([patheffects.withStroke(linewidth=0.25, foreground=label['color'])]
            if plt.rcParams['font.family'][0] == 'Arial Unicode MS' else [])
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    panel = ax.get_window_extent(renderer).padded(-3)
    points = ax.transData.transform([(item['x'], item['y']) for item in labels])
    radii = np.sqrt([item['marker_size'] for item in labels]) * fig.dpi / 144 + 1.5
    marker_boxes = np.column_stack((points - radii[:, None], points + radii[:, None]))

    def overlap(boxes, obstacles):
        extent = np.maximum(0, np.minimum(boxes[:, None, 2:], obstacles[None, :, 2:])
                            - np.maximum(boxes[:, None, :2], obstacles[None, :, :2]))
        return np.prod(extent, axis=2)

    candidates = []
    angles = np.arange(32) * 2 * np.pi / 32
    directions = np.column_stack((np.cos(angles), np.sin(angles)))
    for label, point, radius in zip(labels, points, radii):
        bounds = label['annotation'].get_window_extent(renderer)
        half = np.array([bounds.width / 2 + 1.5, bounds.height / 2 + 1.5])
        offsets = []
        for gap in (2.4, 8, 16, 24, 32, 40, 48):
            distance = radius + gap * fig.dpi / 72
            for sign in (1, -1):
                offsets.extend((sign * (half[0] + distance), y) for y in np.linspace(-half[1], half[1], 9))
                offsets.extend((x, sign * (half[1] + distance)) for x in np.linspace(-half[0], half[0], 17))
            offsets.extend(np.sign(directions) * half + directions * distance)
        offsets = np.array(offsets)
        centers = point + offsets
        boxes = np.column_stack((centers - half, centers + half))
        inside = ((boxes[:, 0] >= panel.x0) & (boxes[:, 1] >= panel.y0)
                  & (boxes[:, 2] <= panel.x1) & (boxes[:, 3] <= panel.y1))
        valid = inside & (overlap(boxes, marker_boxes).sum(axis=1) == 0)
        boxes, offsets = boxes[valid], offsets[valid]
        if not len(boxes):
            raise RuntimeError(f"Diagnostic label layout is too dense: {label['text']}")
        edge = np.linalg.norm(np.maximum(np.abs(offsets) - half, 0), axis=1)
        candidates.append((boxes, offsets, edge + 0.001 * np.linalg.norm(offsets, axis=1)))

    chosen = np.zeros(len(labels), dtype=int)
    placed = np.zeros((len(labels), 4))
    order = np.argsort([len(item[0]) for item in candidates], kind='stable')
    active = []
    for index in order:
        boxes, _, preference = candidates[index]
        cost = overlap(boxes, placed[active]).sum(axis=1) if active else np.zeros(len(boxes))
        chosen[index] = int(np.argmin(cost * 1000 + preference))
        placed[index] = boxes[chosen[index]]
        active.append(index)
    rng = np.random.default_rng(1729)
    for attempt in range(max(200, len(labels) * 60)):
        conflicts = overlap(placed, placed)
        np.fill_diagonal(conflicts, 0)
        bad = np.flatnonzero(conflicts.sum(axis=1) > 0)
        if not len(bad):
            break
        index = int(rng.choice(bad))
        boxes, _, preference = candidates[index]
        areas = overlap(boxes, np.delete(placed, index, axis=0))
        counts = (areas > 0).sum(axis=1)
        choices = np.flatnonzero(counts == counts.min())
        choice = choices[np.argmin(preference[choices])] if counts.min() == 0 else rng.choice(choices)
        chosen[index] = int(choice)
        placed[index] = boxes[choice]
    else:
        raise RuntimeError('Diagnostic label layout is too dense: compact label collisions')
    # Pull labels back toward their dots after resolving crowded groups.
    for _ in range(3):
        changed = False
        order = sorted(range(len(labels)), key=lambda i: candidates[i][2][chosen[i]], reverse=True)
        for index in order:
            boxes, _, preference = candidates[index]
            clear = np.flatnonzero(overlap(boxes, np.delete(placed, index, axis=0)).sum(axis=1) == 0)
            choice = int(clear[np.argmin(preference[clear])])
            if preference[choice] < preference[chosen[index]] - 1e-8:
                chosen[index] = choice
                placed[index] = boxes[choice]
                changed = True
        if not changed:
            break
    for index, label in enumerate(labels):
        label['annotation'].set_position(tuple(candidates[index][1][chosen[index]] * 72 / fig.dpi))
        box = placed[index]
        edge = np.clip(points[index], box[:2], box[2:])
        if np.linalg.norm(edge - points[index]) > radii[index] + 8:
            start, end = ax.transData.inverted().transform([points[index], edge])
            ax.plot([start[0], end[0]], [start[1], end[1]], color='#a8a8a8', linewidth=0.55, zorder=2)

def place_exposure_labels(fig, ax, labels):
    """Expand a square page when dense multilingual labels need more room."""
    original_lines = set(ax.lines)
    for attempt in range(7):
        try:
            _adjacent_exposure_labels(fig, ax, labels)
            return
        except RuntimeError as error:
            if 'layout is too dense' not in str(error) or attempt == 6:
                raise
            for line in list(ax.lines):
                if line not in original_lines:
                    line.remove()
            side = float(max(fig.get_size_inches())) * 1.18
            fig.set_size_inches(side, side)


def finish_diagnostic(fig, ax, labels):
    """Keep the figure and plotting panel physically square, including exports."""
    side = max(15.0, 15.0 * math.sqrt(len(labels) / 100))
    fig.set_size_inches(side, side)
    fig.set_layout_engine(None)
    fig.subplots_adjust(left=0.12, right=0.92, bottom=0.13, top=0.93)
    ax.set_box_aspect(1)
    ax.set_xscale('linear')
    from matplotlib.ticker import AutoLocator, ScalarFormatter, NullLocator
    ax.xaxis.set_major_locator(AutoLocator())
    ax.xaxis.set_major_formatter(ScalarFormatter())
    ax.xaxis.set_minor_locator(NullLocator())
    # Include the null/reference line even when every point is on one side.
    references = [float(line.get_xdata()[0]) for line in ax.lines
                  if line.is_dashed() and len(line.get_xdata()) == 2
                  and line.get_xdata()[0] == line.get_xdata()[1]]
    xs = [float(label['x']) for label in labels] + references
    if xs:
        low, high = min(xs), max(xs)
        span = max(high - low, 0.2)
        ax.set_xlim(low - 0.12 * span, high + 0.18 * span)
    ax.set_ylim(-5, 105)
    ax.set_yticks(np.arange(0, 101, 20))
    ax.set_title(wrap_label(ax.get_title(), 80), fontsize=22, fontweight='bold', color='#222222', pad=20)
    ax.xaxis.label.set_fontsize(18); ax.xaxis.label.set_fontweight('normal')
    ax.yaxis.label.set_fontsize(18); ax.yaxis.label.set_fontweight('normal')
    ax.xaxis.labelpad=14; ax.yaxis.labelpad=14
    ax.tick_params(axis='both', labelsize=15, length=0, pad=8, colors='#444444')
    for tick in (*ax.get_xticklabels(), *ax.get_yticklabels()): tick.set_fontweight('normal')
    for spine in ax.spines.values(): spine.set_visible(False)
    ax.set_axisbelow(True); ax.grid(color='#e5e5e5', linestyle='-', linewidth=0.8)
    legend=ax.get_legend()
    if legend is not None: legend.remove()
    fig.patch.set_facecolor('white')
    place_exposure_labels(fig, ax, labels)


def save_diagnostic(fig, path):
    """Export without tight cropping, which would make square pages rectangular."""
    path=Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    notes=[]
    for item in fig.texts:
        if item is fig._suptitle: continue
        if not item.get_text(): continue
        notes.append(item)
    # Captions and scope notes stay inside the square page.
    y=0.018
    for item in reversed(notes):
        text=wrap_label(item.get_text(), 150)
        item.set_text(text); item.set_position((0.12, y)); item.set_ha('left'); item.set_va('bottom')
        item.set_fontsize(11); y += 0.015 * (text.count('\n') + 1)
    fig.savefig(path.with_suffix('.pdf'), format='pdf', metadata={'CreationDate':None,'ModDate':None})
    fig.savefig(path.with_suffix('.png'), format='png', dpi=150)

STYLE_STRINGS = {'egger_heterogeneity_title': {'en': "Egger's test log10(p-value) vs heterogeneity - {disease}", 'zh-CN': 'Egger 检验 log10(p 值) 与异质性 - {disease}', 'es': 'log10(valor p) de la prueba de Egger frente a la heterogeneidad - {disease}', 'fr': "log10(valeur p) du test d'Egger en fonction de l'hétérogénéité - {disease}", 'zh-TW': 'Egger 檢定 log10(p 值) 與異質性 - {disease}', 'nl': 'log10(p-waarde) van Eggers toets versus heterogeniteit - {disease}', 'ko': 'Egger 검정 log10(p값)과 이질성 - {disease}'}, 'egger_x': {'en': "log10(Egger's p-value)", 'zh-CN': 'log10(Egger 检验 p 值)', 'es': 'log10(valor p de la prueba de Egger)', 'fr': "log10(valeur p du test d'Egger)", 'zh-TW': 'log10(Egger 檢定 p 值)', 'nl': 'log10(p-waarde van Eggers toets)', 'ko': 'log10(Egger 검정 p값)'}, 'effect_diagnostic_caption': {'en': 'Point size reflects the number of studies; colors identify individual exposures; the dashed line marks RR = 1.', 'zh-CN': '点大小反映研究数量；颜色区分各暴露因素；虚线标示 RR = 1。', 'es': 'El tamaño del punto refleja el número de estudios; los colores identifican cada exposición; la línea discontinua marca RR = 1.', 'fr': "La taille du point reflète le nombre d'études ; les couleurs identifient chaque exposition ; la ligne en tirets marque RR = 1.", 'zh-TW': '點大小反映研究數量；顏色區分各暴露因子；虛線標示 RR = 1。', 'nl': 'De puntgrootte weerspiegelt het aantal onderzoeken; kleuren onderscheiden afzonderlijke blootstellingen; de streepjeslijn markeert RR = 1.', 'ko': '점 크기는 연구 수를 반영하며, 색상은 개별 노출 요인을 나타내고, 점선은 RR = 1을 표시합니다.'}, 'egger_diagnostic_caption': {'en': "Point size reflects the number of studies; colors identify individual exposures; the dashed line marks Egger's p = 0.05.", 'zh-CN': '点大小反映研究数量；颜色区分各暴露因素；虚线标示 Egger 检验 p = 0.05。', 'es': 'El tamaño del punto refleja el número de estudios; los colores identifican cada exposición; la línea discontinua marca p de Egger = 0,05.', 'fr': "La taille du point reflète le nombre d'études ; les couleurs identifient chaque exposition ; la ligne en tirets marque p d'Egger = 0,05.", 'zh-TW': '點大小反映研究數量；顏色區分各暴露因子；虛線標示 Egger 檢定 p = 0.05。', 'nl': 'De puntgrootte weerspiegelt het aantal onderzoeken; kleuren onderscheiden afzonderlijke blootstellingen; de streepjeslijn markeert Eggers p = 0,05.', 'ko': '점 크기는 연구 수를 반영하며, 색상은 개별 노출 요인을 나타내고, 점선은 Egger 검정 p = 0.05를 표시합니다.'}}
