"""Explicit local Book voice choices over the existing single registry.

Book requests copy supported controls into immutable Role data. They do not
load another preference store, infer a language, or select a fallback voice.
"""


def select_voice(voices, choice):
    if (type(choice) is not dict or not {'language', 'voice_id'} <= set(choice)
            or not set(choice) <= {'language', 'voice_id', 'rate', 'volume'}
            or type(choice['language']) is not str or not choice['language'].strip()
            or type(choice['voice_id']) is not str or not choice['voice_id'].strip()):
        raise ValueError('Choose an explicit installed voice ID, its declared language and supported controls.')
    matches = [voice for voice in voices if voice.id == choice['voice_id']]
    if len(matches) != 1:
        raise ValueError('The selected local voice is unavailable or ambiguous.')
    voice = matches[0]
    if choice['language'] != voice.language:
        raise ValueError('The requested language differs from the selected voice declaration.')
    rate, volume = choice.get('rate', 0), choice.get('volume', 100)
    if type(rate) is not int or not -10 <= rate <= 10 or type(volume) is not int or not 0 <= volume <= 100:
        raise ValueError('Choose a supported integer voice rate and volume.')
    capabilities = getattr(voice, 'capabilities', ())
    if rate != 0 and 'rate' not in capabilities or volume != 100 and 'volume' not in capabilities:
        raise ValueError('This installed voice does not support the selected rate or volume.')
    return dict(voice_id=voice.id, language=voice.language, rate=rate, volume=volume)
