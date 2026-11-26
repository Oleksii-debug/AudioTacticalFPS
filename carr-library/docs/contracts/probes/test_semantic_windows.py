"""Independent source-window contract probe; no model inference or production changes."""
import hashlib
from importlib.metadata import version
from pathlib import Path
import re

import pytest
from nltk.tokenize import PunktSentenceTokenizer, punkt
from nltk.tokenize.punkt import PunktParameters


def splitter():
    params = PunktParameters()
    # Small synthetic fixture profile, not a supported language dictionary.
    params.abbrev_types = {'dr', 'e', 'h', 'p', 'prof', 'napr', 'č', 'проф', 'ст', 'ім', 'м'}
    return PunktSentenceTokenizer(params)


def test_exact_upstream():
    source = Path(punkt.__file__).read_bytes()
    assert version('nltk') == '3.9.2'
    assert hashlib.sha1(b'blob ' + str(len(source)).encode() + b'\0' + source).hexdigest() == '877b79dd29103464d13c967cc74e030a92c7cfbc'


@pytest.mark.parametrize('raw,tail', [
    ('Dr. Smith studied E. H. Carr. See p. 42. Railways expanded.', 'Railways expanded.'),
    ('Проф. Іваненко жив у м. Київ. Далі дослідження.', 'Далі дослідження.'),
    ('Prof. Novák žil v meste Nitra. Pozri napr. stranu 42. Ďalšia veta.', 'Ďalšia veta.'),
    ('  Cafe\u0301.\n\nČaj.\tКнига.  ', 'Книга.'),
])
def test_native_spans(raw, tail):
    spans = list(splitter().span_tokenize(raw))
    assert len(spans) >= 2
    pos = 0
    for start, end in spans:
        assert pos <= start < end <= len(raw)
        assert not raw[pos:start].strip()  # no omitted non-whitespace text
        pos = end
    assert not raw[pos:].strip()
    assert raw[slice(*spans[-1])] == tail
    if '\u0301' in raw:
        assert '\u0301' in raw[slice(*spans[0])]


def test_sentence_only_cannot_enforce_model_budget():
    raw = ' '.join(['railway'] * 180) + ' archive.'
    spans = list(splitter().span_tokenize(raw))
    assert spans == [(0, len(raw))]
    # Lower-bound illustrative word count, NOT the model's subword token count.
    assert len(raw.split()) > 128


def test_sentence_only_not_universal():
    raw = '中文。第二句。日本語。'
    assert list(splitter().span_tokenize(raw)) == [(0, len(raw))]


def test_prefix_only_backend_can_miss_tail():
    raw = ' '.join(['ordinary'] * 180) + '.\nRailway reform.'
    # Deliberately synthetic prefix-limited ranker, not MiniLM or a tokenizer.
    def sees_target(text):
        return 'Railway' in text.split()[:128]
    assert not sees_target(raw)
    spans = list(splitter().span_tokenize(raw))
    matching = [(a, b) for a, b in spans if sees_target(raw[a:b])]
    assert len(matching) == 1
    a, b = matching[0]
    assert raw[a:b] == 'Railway reform.'
    assert raw[a:b] != raw
