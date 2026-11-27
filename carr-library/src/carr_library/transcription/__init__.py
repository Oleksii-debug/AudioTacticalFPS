"""Optional adapter to a separately supplied, pinned local transcription worker.

This package contains no ASR model, private worker or model downloader.
Transcripts are derivatives; they never establish quotation authority.
"""
from .models import (TranscriptionOptions, TranscriptionBudget, TranscriptionControl,
                     PreparedTranscription, TranscriptionReceipt, TranscriptionProgress)
from .runtime import TranscriptionRuntime
from .service import TranscriptionService

__all__ = ['TranscriptionOptions', 'TranscriptionBudget', 'TranscriptionControl',
           'PreparedTranscription', 'TranscriptionReceipt', 'TranscriptionProgress',
           'TranscriptionRuntime', 'TranscriptionService']
