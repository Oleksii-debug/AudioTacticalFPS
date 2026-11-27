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
