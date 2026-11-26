"""Distinct spoken labels for already-authorized library/topic choices.

The caller retains the original row order and identity mapping. Formatting
never grants access, changes a title, or refreshes a research view.
"""
from collections import Counter


def scope_labels(rows):
    rows = tuple(rows)
    label_counts = Counter(label for _, _, label in rows)
    rendered = set()
    labels = []
    for workspace, topic, label in rows:
        if label_counts[label] > 1:
            kind = 'library only' if topic is None else f'topic {topic}'
            label = f'{label} (library {workspace}; {kind})'
        original, number = label, 1
        while label in rendered:
            number += 1
            label = f'{original} (choice {number})'
        rendered.add(label)
        labels.append(label)
    return labels
