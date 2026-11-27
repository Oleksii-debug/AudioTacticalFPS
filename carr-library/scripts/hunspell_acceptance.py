"""Actual explicitly selected Hunspell acceptance, using original tiny dictionaries.

This is separate from engine-independent source tests. It never downloads a
library or a language pack, and it does not claim dictionary/language quality.
"""
import argparse
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory


def accept(library):
    from carr_library.speech import spelling
    from carr_library.speech.reading import ReadingDocument
    from carr_library.ui.spelling_dialog import (
        ReadingSnapshot, SpellingReview, apply_correction, preview_correction,
    )
    checks = []
    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)

    with TemporaryDirectory(prefix='hunspell-acceptance-') as temporary:
        root = Path(temporary) / 'Словники 😀'; root.mkdir()
        affix, dictionary = root / 'український.aff', root / 'український.dic'
        affix.write_text('SET UTF-8\nFLAG UTF-8\nTRY abcdefghijklmnopqrstuvwxyz\n'
                         'SFX S Y 1\nSFX S 0 s .\n', encoding='utf-8')
        dictionary.write_text('5\nhello\nworld/S\nretained\nслово\nУкраїна\n', encoding='utf-8')
        original = affix.read_bytes(), dictionary.read_bytes()
        profile = spelling.review_hunspell(library, affix, dictionary, language='synthetic en+uk',
            provenance='Original acceptance dictionaries only. Explicit external native library; no redistribution approval.')
        check('exact selected native library hash', profile.library.sha256 == sha256(Path(library).read_bytes()).hexdigest())
        text = '😀 hello\r\nwrld worlds Україна слово'
        result = spelling.check_spelling(text, profile)
        check('actual C API, UTF-8 dictionary and affix expansion', result.words == 5 and result.encoding == 'UTF-8'
              and len(result.issues) == 1 and result.issues[0].word == 'wrld'
              and 'world' in result.issues[0].suggestions)
        check('exact Unicode/CRLF reading offsets', (result.issues[0].start, result.issues[0].end) == (9, 13))
        selected = spelling.check_spelling(text, profile, bounds=(text.index('Україна'), len(text)))
        check('selected-text scope with original character positions', selected.words == 2 and not selected.issues)
        long_word = spelling.check_spelling('a' * 129, profile)
        check('overlong tokens are explicitly uncheckable', len(long_word.issues) == 1
              and long_word.issues[0].reason == 'too_long')
        document = ReadingDocument(text)
        snapshot = ReadingSnapshot.capture(document, (9, 13))
        preview = preview_correction(SpellingReview(snapshot, result), 0, 'world', snapshot)
        check('actual native result remains review-only', document.text == text and document.revision == 0)
        apply_correction(document, preview, snapshot)
        check('explicit correction preserves source and one undo', document.text == text.replace('wrld', 'world')
              and document.source_text == text and document.revision == 1 and document.undo() and document.text == text)
        check('external dictionary bytes remain unchanged', (affix.read_bytes(), dictionary.read_bytes()) == original)

        legacy_aff, legacy_dic = root / 'legacy.aff', root / 'legacy.dic'
        legacy_aff.write_bytes(b'SET ISO8859-1\nTRY abcdefghijklmnopqrstuvwxyz\n')
        legacy_dic.write_bytes('2\nhello\ncafé\n'.encode('iso8859-1'))
        legacy = spelling.review_hunspell(library, legacy_aff, legacy_dic, language='synthetic legacy',
                                          provenance='Original two-word ISO8859-1 fixture')
        legacy_result = spelling.check_spelling('café Україна', legacy)
        check('declared legacy encoding with no replacement decoding', len(legacy_result.issues) == 1
              and legacy_result.issues[0].word == 'Україна' and legacy_result.issues[0].reason == 'unencodable')
        dictionary.write_bytes(original[1].replace(b'hello', b'other'))
        try:
            spelling.check_spelling('hello', profile)
        except ValueError as exc:
            check('same-size changed dictionary is rejected before use', 'changed' in str(exc))
        else:
            raise AssertionError('Changed dictionary was accepted.')
        dictionary.write_bytes(original[1])
        affix.write_bytes(b'This is not a Hunspell affix declaration.\n')
        invalid = spelling.review_hunspell(library, affix, dictionary, language='synthetic invalid',
                                           provenance='Deliberately malformed original test fixture')
        try:
            spelling.check_spelling('hello', invalid)
        except ValueError as exc:
            check('malformed dictionary cannot report a complete review', 'dictionary' in str(exc))
        else:
            raise AssertionError('Malformed dictionary was accepted.')
        affix.write_bytes(original[0])

        # Long native dictionary initialization guarantees a live owned child.
        large = root / 'large.dic'
        with large.open('w', encoding='utf-8') as stream:
            stream.write('500001\nhello\n')
            for index in range(500000):
                stream.write(f'syntheticword{index}\n')
        large_profile = spelling.review_hunspell(library, affix, large, language='synthetic lifecycle',
                                                provenance='Original generated lifecycle fixture')
        actual_run = spelling.run_process
        observed = []
        def observe(*args, **kwargs):
            observed.append(Path(kwargs['cwd']))
            return actual_run(*args, **kwargs)
        spelling.run_process = observe
        try:
            try:
                spelling.check_spelling('hello', large_profile,
                    cancelled=lambda: bool(observed and (observed[-1] / 'dictionary.dic').exists()))
            except InterruptedError:
                check('actual mid-staging/check cancellation reaps and removes private copies',
                      bool(observed) and not observed[-1].exists())
            else:
                raise AssertionError('The active native worker did not observe cancellation.')
            try:
                spelling.check_spelling('hello', large_profile, timeout=.001)
            except TimeoutError:
                check('actual worker deadline reaps and removes private copies', len(observed) == 2
                      and not observed[-1].exists())
            else:
                raise AssertionError('The active native worker did not reach its deadline.')
        finally:
            spelling.run_process = actual_run
        check('native engine bytes remain unchanged', profile.library.sha256 == sha256(Path(library).read_bytes()).hexdigest())

    import carr_library.speech.spelling_worker as worker_module
    import carr_library.ui.spelling_dialog as dialog_module
    modules = (spelling, worker_module, dialog_module)
    return {'status': 'PASS', 'checks': checks, 'native_library_sha256': profile.library.sha256,
            'native_library_bytes': profile.library.size,
            'application_modules': {module.__name__: sha256(Path(module.__file__).read_bytes()).hexdigest()
                                    for module in modules},
            'dictionaries': 'Original synthetic fixtures; no language-quality qualification',
            'native_windows': 'NOT_RUN' if __import__('os').name != 'nt' else 'EXECUTED',
            'manual_nvda': 'NOT_RUN', 'distribution_clearance': 'NOT_GRANTED'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', required=True, help='Explicit reviewed standalone Hunspell native library')
    parser.add_argument('--output', help='New JSON report path; an existing report is never replaced')
    args = parser.parse_args()
    report = accept(args.library)
    payload = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        from carr_library.speech._io import new_output
        with new_output(args.output) as staged:
            staged.write_text(payload, encoding='utf-8')
    print(payload, end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
