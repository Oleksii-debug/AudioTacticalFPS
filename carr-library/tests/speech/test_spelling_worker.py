"""Bounded native protocol without depending on a particular installed dictionary."""
import ctypes
from hashlib import sha256

import pytest

from carr_library.speech import spelling_worker as worker


def request(text='word', language='en-US', operation='check'):
    return {'version': 1, 'request_id': 'b' * 32, 'operation': operation, 'text': text,
            'text_sha256': sha256(text.encode()).hexdigest(), 'language': language}


def test_read_only_provider_calls_never_mutate_or_choose_a_fallback():
    calls = []

    class Provider:
        def check(self, text, language):
            calls.append(('check', text, language))
            return []
        def close(self):
            calls.append(('close',))
        def __getattr__(self, name):
            pytest.fail('Unexpected provider API: ' + name)

    value = worker.process_request(request(language='uk-UA'), factory=Provider)
    assert value['status'] == 'ok' and value['language'] == 'uk-UA'
    assert calls == [('check', 'word', 'uk-UA'), ('close',)]


def test_unsupported_selected_language_is_reported_without_trying_another():
    calls = []

    class Provider:
        def check(self, text, language):
            calls.append(language)
            raise worker.NativeFailure('unsupported_language')
        def close(self):
            calls.append('closed')

    value = worker.process_request(request(language='uk-UA'), factory=Provider)
    assert value['error'] == 'unsupported_language'
    assert calls == ['uk-UA', 'closed']


def test_provider_failure_does_not_echo_private_text_or_paths():
    class Provider:
        def check(self, text, language):
            raise ValueError('Secret source and private profile path')
        def close(self):
            pass

    value = worker.process_request(request(), factory=Provider)
    assert value['error'] == 'native_failure' and 'Secret' not in str(value)


@pytest.mark.parametrize('change', [
    ('version', True), ('request_id', 'A' * 32), ('text_sha256', 'f' * 64),
    ('text', 'word\x00'), ('language', 'en US'), ('operation', 'add'), ('extra', 1),
])
def test_invalid_requests_never_initialize_com(change):
    value = request()
    value[change[0]] = change[1]
    with pytest.raises(worker.NativeFailure):
        worker.process_request(value, factory=lambda: pytest.fail('COM was opened'))


def test_unknown_native_output_cannot_escape_reply_budget():
    class Provider:
        def check(self, text, language):
            return ['x' * (worker.MAX_REPLY_BYTES + 1)]
        def close(self):
            pass

    value = worker.process_request(request(), factory=Provider)
    assert value['status'] == 'error' and value['error'] == 'capacity'
    assert 'result' not in value


def _enumerator(items):
    """Model the documented Next and CoTaskMemFree ownership contract."""
    instance = worker.WindowsSpeller.__new__(worker.WindowsSpeller)
    strings, freed, released, calls = [], [], [], []
    instance.ole = type('Ole', (), {'CoTaskMemFree': staticmethod(lambda p: freed.append(p.value))})()
    iterator = iter(items)

    def call(pointer, slot, types, count, output, fetched):
        assert slot == 3 and count == 1
        calls.append('next')
        try:
            item = next(iterator)
        except StopIteration:
            ctypes.cast(fetched, ctypes.POINTER(ctypes.c_uint32))[0] = 0
            return 1
        buffer = ctypes.create_unicode_buffer(item)
        strings.append(buffer)
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(buffer)
        ctypes.cast(fetched, ctypes.POINTER(ctypes.c_uint32))[0] = 1
        return 0

    instance._call = call
    instance._release = lambda pointer: released.append(pointer.value)
    return instance, freed, released, calls


def test_suggestions_are_explicitly_limited_and_every_consumed_string_is_freed():
    instance, freed, released, calls = _enumerator(['word' + str(i) for i in range(40)])
    values, limited = instance._strings(ctypes.c_void_p(123), 12, 256, truncate=True)
    assert len(values) == 12 and limited is True
    assert len(calls) == 13 and len(freed) == 13 and released == [123]


def test_language_overflow_fails_instead_of_hiding_installed_entries():
    instance, freed, released, calls = _enumerator(['en-US', 'uk-UA', 'fr-FR'])
    with pytest.raises(worker.NativeFailure) as caught:
        instance._strings(ctypes.c_void_p(123), 2, 64)
    assert caught.value.code == 'capacity'
    assert len(freed) == 3 and released == [123]


def test_overlong_com_string_is_freed_and_enumerator_released():
    instance, freed, released, calls = _enumerator(['x' * 257])
    with pytest.raises(worker.NativeFailure) as caught:
        instance._strings(ctypes.c_void_p(123), 12, 256, truncate=True)
    assert caught.value.code == 'capacity' and len(freed) == 1 and released == [123]


def test_guid_representation_matches_sdk_and_windows_widths():
    import uuid
    assert ctypes.sizeof(worker.GUID) == 16
    assert bytes(worker.GUID.parse(worker.IID_FACTORY)) == uuid.UUID(
        '8E018A9D-2415-4677-BF08-794EA61F94BB').bytes_le
    assert ctypes.sizeof(ctypes.c_int32) == ctypes.sizeof(ctypes.c_uint32) == 4
