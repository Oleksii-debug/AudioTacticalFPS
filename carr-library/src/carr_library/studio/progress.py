"""Observe accepted Studio speech chunks through the shared progress contract.

Speech counts describe synthesis work only. Mixing, music and durable output
publication remain explicit phases; the UI confirms success only after Director
returns its committed production result. No word timing or total duration is
inferred from text length.
"""
from ..progress import current_progress


def _message(value, maximum):
    return ''.join(char if ord(char) >= 32 and ord(char) != 127 or char == '\n'
                   else ' ' for char in str(value))[:maximum]


def report_render_progress(state, task=None):
    """Optional observation cannot turn a retained render into a failed job."""
    task = task if task is not None else current_progress()
    if task is None:
        return
    try:
        previous = task.snapshot()
        if previous.terminal:
            return
        completed, total = state['speech_chunks_done'], state['speech_chunks_total']
        if type(completed) is not int or type(total) is not int or not 0 <= completed <= total:
            raise ValueError('Studio reported invalid accepted speech chunk counts.')
        # Each render/resume has its own measurement: the selected engine may
        # have changed despite an unchanged script revision.
        task.update(completed=completed, total=total or None,
                    phase=_message(state['phase'], 200),
                    message=_message(state['message'], 1000))
    except Exception as error:
        try:
            if not task.snapshot().terminal:
                task.unavailable(_message('Studio progress reporting is unavailable: ' + str(error), 1000))
        except Exception:
            pass
