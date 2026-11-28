"""Source-specific ASR reading drafts; no recognition text grants quote authority."""
import re

from ..audio.models import AudioCancelled


def prepare_reading_copy(library_root, service, job_id, source_file, *,
                         edited_text=None, cancelled=lambda: False):
    """Atomically seed one derivative; existing edits, voice and undo win."""
    from ..speech.reading import ReadingDocument
    from ..speech.session import ReaderDraftStore, ReaderResume
    legacy = re.fullmatch('[0-9a-f]{64}', job_id) is not None
    if legacy:
        detail = service.details(job_id, cancelled=cancelled)
        sources = {item['file'] for item in detail['record']['request']['inputs']}
    else:
        state = service.get_job(job_id)
        sources = {item['id'] for item in state['sources']}
    if source_file not in sources:
        raise ValueError('Select an input belonging to this transcription job.')
    slot = f'transcription:{job_id}:{source_file}'
    store = ReaderDraftStore(library_root, slot=slot)
    saved = store.load()
    if saved is not None:
        # Recovery of the user's derivative does not certify an ASR receipt.
        return {'slot': slot, 'recovered': True}
    if legacy:
        text, label = service.transcript_text(job_id, source_file, cancelled=cancelled)
    else:
        if cancelled():
            raise AudioCancelled('Opening the reading copy was cancelled.')
        value = service.read_transcript(job_id, source_file, cancel=cancelled)
        text = value['text']
        label = next(item.get('name', source_file) for item in state['sources'] if item['id'] == source_file)
    if cancelled():
        raise AudioCancelled('Opening the reading copy was cancelled.')
    label = ' '.join(str(label).split())
    candidate = ReaderResume(ReadingDocument(text, title=('Transcript reading copy: ' + label)[:512]))
    if edited_text is not None:
        candidate.document.set_text(edited_text, label='Transcript reading-copy edit')
    saved = store.save_if_absent(candidate)
    return {'slot': slot, 'recovered': saved is not candidate}
