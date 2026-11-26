"""Local speech and source-preserving editable reading copies.

Importing this package does not enumerate voices, start playback, load a model,
or access the network. Discovery and synthesis are explicit actions.
"""
from .runtime import (EspeakProvider, FliteProvider, LocalHelperProvider, PiperProvider,
                      SapiProvider, SpeechResult, SpeechSettings, Voice, VoiceRegistry,
                      split_utterances, synthesize)
from .reading import (PronunciationRule, ReadingDocument, SourceSpan,
                      export_dictionary, import_dictionary)

__all__ = ['Voice', 'VoiceRegistry', 'SpeechSettings', 'SpeechResult', 'synthesize',
           'split_utterances', 'EspeakProvider', 'FliteProvider', 'PiperProvider',
           'SapiProvider', 'LocalHelperProvider', 'ReadingDocument', 'SourceSpan',
           'PronunciationRule', 'export_dictionary', 'import_dictionary']
