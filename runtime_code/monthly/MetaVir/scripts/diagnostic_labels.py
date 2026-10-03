"""Compact placement for enlarged labels on existing diagnostic pages."""
import re
import textwrap
import numpy as np
import matplotlib.pyplot as plt
from matplotlib import patheffects

def wrap_label(value, width=95):
    width = max(12, width // 2) if re.search(r'[\u3000-\u9fff\uac00-\ud7af]', str(value)) else width
    return '\n'.join(textwrap.fill(line, width=width, break_long_words=True, break_on_hyphens=False) for line in str(value).splitlines())


def _solve_label_candidates(candidates):
    """Resolve crowded label choices with deterministic forward checking."""
    candidates = list(candidates)
    source_indices = [np.arange(len(item[0])) for item in candidates]
    for index, (boxes, offsets, preference) in enumerate(candidates):
        if len(boxes) <= 64:
            continue
        selected = [int(np.argmin(preference))]
        distance = np.full(len(boxes), np.inf)
        while len(selected) < 64:
            distance = np.minimum(distance, np.linalg.norm(offsets - offsets[selected[-1]], axis=1))
            distance[selected] = -1
            selected.append(int(np.argmax(distance)))
        selected = np.array(selected)
        source_indices[index] = selected
        candidates[index] = (boxes[selected], offsets[selected], preference[selected])
    size = len(candidates)
    conflicts = [[{} for _ in item[0]] for item in candidates]
    for i, (first, _, _) in enumerate(candidates):
        for j in range(i + 1, size):
            second = candidates[j][0]
            intersections = ((first[:, None, 0] < second[None, :, 2])
                             & (first[:, None, 2] > second[None, :, 0])
                             & (first[:, None, 1] < second[None, :, 3])
                             & (first[:, None, 3] > second[None, :, 1]))
            for index, row in enumerate(np.packbits(intersections, axis=1, bitorder='little')):
                mask = int.from_bytes(row.tobytes(), 'little')
                if mask:
                    conflicts[i][index][j] = mask
            for index, row in enumerate(np.packbits(intersections.T, axis=1, bitorder='little')):
                mask = int.from_bytes(row.tobytes(), 'little')
                if mask:
                    conflicts[j][index][i] = mask
    nodes = 0
    solution = [-1] * size

    def search(domains, remaining):
        nonlocal nodes
        nodes += 1
        if nodes > 50000:
            raise TimeoutError
        if not remaining:
            return True
        index = min(remaining, key=lambda i: (domains[i].bit_count(), -len(conflicts[i][0])))
        others = remaining - {index}
        options = []
        bits = domains[index]
        while bits:
            first = bits & -bits
            candidate = first.bit_length() - 1
            bits ^= first
            forbidden = conflicts[index][candidate]
            updated = domains.copy()
            loss = 0
            for j, mask in forbidden.items():
                if j not in others:
                    continue
                updated[j] &= ~mask
                if not updated[j]:
                    break
                loss += (domains[j] & mask).bit_count() / domains[j].bit_count()
            else:
                options.append((loss, candidates[index][2][candidate], candidate, updated))
        for _, _, candidate, updated in sorted(options, key=lambda item: (item[0], item[1])):
            solution[index] = candidate
            if search(updated, others):
                return True
        return False
    try:
        success = search([(1 << len(item[0])) - 1 for item in candidates], set(range(size)))
    except TimeoutError:
        success = False
    if not success:
        raise RuntimeError('Diagnostic label layout is too dense: candidate solver')
    return np.array([source_indices[i][choice] for i, choice in enumerate(solution)])


def _adjacent_exposure_labels(fig, ax, labels, font_size=14, max_gap=72):
    """Keep every label near its marker while avoiding labels and other dots.

    Search label edges, rather than text centers, so a long exposure name can
    sit directly beside its point. All candidates have a bounded edge gap;
    crowded groups use coordinated choices while the page size stays fixed.
    """
    if not labels:
        return
    for label in labels:
        label['text'] = wrap_label(label.setdefault('source_text', label['text']), 42)
        if 'annotation' not in label:
            label['annotation'] = ax.annotate(label['text'], (label['x'], label['y']),
                xytext=(0, 0), textcoords='offset points', ha='center', va='center',
                color=label['color'], zorder=5)
        annotation = label['annotation']
        annotation.set_position((0, 0))
        annotation.set_text(label['text'])
        annotation.set_fontsize(font_size)
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
        for gap in (2.4, *range(8, max_gap + 1, 8)):
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

    chosen = _solve_label_candidates(candidates)
    placed = np.array([candidates[i][0][choice] for i, choice in enumerate(chosen)])
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


def place_labels(fig, ax, labels):
    for gap in (32, 48, 64, 80):
        try:
            _adjacent_exposure_labels(fig, ax, labels, font_size=11, max_gap=gap)
            return
        except RuntimeError as error:
            if 'layout is too dense' not in str(error):
                raise
            last_error = error
    raise last_error
