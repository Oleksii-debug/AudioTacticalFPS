"""Gold schema and metric invariants; no inference or quality claim."""
import importlib.util
import json
from pathlib import Path

import pytest


script = Path(__file__).resolve().parents[2] / 'scripts/evaluate_retrieval.py'
spec = importlib.util.spec_from_file_location('evaluate_retrieval', script)
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


def fixture():
    return {'schema': 'carr-retrieval-gold/1',
            'documents': [{'id': 'a', 'text': 'One'}, {'id': 'b', 'text': 'Two'}],
            'queries': [{'language': 'uk', 'text': 'Питання', 'relevant': ['a']},
                        {'language': 'en', 'text': 'No match', 'relevant': []}]}


def load(data):
    return evaluation.load_gold(json.dumps(data).encode())


def test_valid_fixture():
    assert load(fixture())[1][0] == ('uk', 'Питання', {'a'})


@pytest.mark.parametrize('mutation', ['duplicate_id', 'duplicate_text', 'unknown_relevance',
                                      'duplicate_relevance', 'no_negative', 'no_positive', 'multiline'])
def test_invalid_gold(mutation):
    data = fixture()
    if mutation == 'duplicate_id':
        data['documents'][1]['id'] = 'a'
    elif mutation == 'duplicate_text':
        data['documents'][1]['text'] = 'One'
    elif mutation == 'unknown_relevance':
        data['queries'][0]['relevant'] = ['unknown']
    elif mutation == 'duplicate_relevance':
        data['queries'][0]['relevant'] = ['a', 'a']
    elif mutation == 'no_negative':
        data['queries'].pop()
    elif mutation == 'no_positive':
        data['queries'].pop(0)
    else:
        data['documents'][0]['text'] = 'One\nTwo'
    with pytest.raises(ValueError):
        load(data)


@pytest.mark.parametrize('raw', [b'{"schema":1,"schema":2}', b'x' * (1024 * 1024 + 1)])
def test_fixture_bound_and_duplicate_keys(raw):
    with pytest.raises(ValueError):
        evaluation.load_gold(raw)


def test_duplicate_windows_do_not_inflate_score():
    assert evaluation.score(['x', 'x', 'a', 'b'], {'a', 'b'}, 2) == {
        'recall': .5, 'mrr': .5, 'false_positive': None}


def test_miss_and_negative_queries():
    assert evaluation.score(['x'], {'a'}, 5)['mrr'] == 0
    assert evaluation.score(['x'], set(), 5)['false_positive'] is True
    assert evaluation.score([], set(), 5)['false_positive'] is False


def test_aggregation_separates_negatives():
    rows = [dict(evaluation.score(['a'], {'a'}, 5), milliseconds=1),
            dict(evaluation.score([], set(), 5), milliseconds=2)]
    assert evaluation.aggregate(rows) == {'positive_queries': 1, 'negative_queries': 1,
        'recall': 1, 'mrr': 1, 'negative_false_positive_rate': 0, 'p50_ms': 1.5, 'p95_ms': 2}
    only_negative = evaluation.aggregate(rows[1:])
    assert only_negative['recall'] is None and only_negative['mrr'] is None

@pytest.mark.parametrize('language,relevant', [('uk', ['a']), ('uk', []),
                                               ('en', []), ('en', ['a']), ('UK', ['a'])])
def test_exact_query_cannot_repeat(language, relevant):
    data = fixture()
    data['queries'].append({'language': language, 'text': data['queries'][0]['text'],
                            'relevant': relevant})
    with pytest.raises(ValueError, match='Duplicate gold query text'):
        load(data)


def test_distinct_multilingual_queries_keep_labels():
    data = fixture()
    data['queries'].append({'language': 'SK', 'text': 'Otázka', 'relevant': ['a']})
    assert load(data)[1] == [
        ('uk', 'Питання', {'a'}), ('en', 'No match', set()), ('sk', 'Otázka', {'a'})]


def test_independent_smoke_fixture_identity_and_schema():
    from hashlib import sha256
    raw = (Path(__file__).parent / 'fixtures/retrieval_smoke_uk_en_sk.json').read_bytes()
    assert sha256(raw).hexdigest() == 'fba3139eaf29a3cb70fa300f392ab8441579878eecd6399ec4bad9e6cae7ed71'
    documents, queries = evaluation.load_gold(raw)
    assert len(documents) == 12 and len(queries) == 18
    for language in ('uk', 'en', 'sk'):
        labels = [relevant for lang, _, relevant in queries if lang == language]
        assert len(labels) == 6 and sum(bool(label) for label in labels) == 5
