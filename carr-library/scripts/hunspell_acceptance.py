"""Actual explicitly selected Hunspell acceptance, using original tiny dictionaries.

This is separate from engine-independent source tests. It never downloads a
library or a language pack, and it does not claim dictionary/language quality.
"""
import argparse
from hashlib import sha256
import json
import importlib
import os
import sys
import sysconfig
from pathlib import Path
from tempfile import TemporaryDirectory


def accept(library, *, source_root, require_installed=False):
    from carr_library.speech import spelling
    from carr_library.speech.reading import ReadingDocument
    from carr_library.ui.spelling_dialog import (
        ReadingSnapshot, SpellingReview, apply_correction, preview_correction,
    )
    checks = []
    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        if label not in checks:
            checks.append(label)

    source_root = Path(source_root).resolve(strict=True)
    installed_root = (Path(os.environ['CARR_PROGRAM_DIR']).resolve() if os.environ.get('CARR_PROGRAM_DIR')
                      else Path(sysconfig.get_path('purelib')).resolve())
    origin_names = (
        'carr_library.speech.spelling', 'carr_library.speech.spelling_worker',
        'carr_library.speech.reading', 'carr_library.speech._io',
        'carr_library.ui.spelling_dialog', 'carr_library.ui.pronunciation_dialog',
        'carr_library.audio.models', 'carr_library.audio.process', 'carr_library.audio.cleanup',
        'carr_library.operations.broker', 'carr_library.operations.models',
        'carr_library.durable_state',
    )
    def checked_origins():
        result = {}
        for name in origin_names:
            module = importlib.import_module(name)
            path = Path(module.__file__).resolve(strict=True)
            expected = source_root.joinpath(*name.split('.')).with_suffix('.py')
            if require_installed:
                check('installed origin: ' + name, path.is_relative_to(installed_root)
                      and not path.is_relative_to(source_root))
            digest = sha256(path.read_bytes()).hexdigest()
            check('exact module bytes: ' + name, digest == sha256(expected.read_bytes()).hexdigest())
            result[name] = {'path': str(path), 'sha256': digest,
                            'installed': path.is_relative_to(installed_root) and not path.is_relative_to(source_root)}
        return result
    origins_before = checked_origins()

    with TemporaryDirectory(prefix='hunspell-acceptance-') as temporary:
        root = Path(temporary) / 'Словники 😀'; root.mkdir()
        state_root = root / 'library' / 'state'
        state_root.mkdir(parents=True)
        def review(*args, **options):
            return spelling.review_hunspell(*args, state_root=state_root, **options)
        def run(*args, **options):
            return spelling.check_spelling(*args, state_root=state_root, **options)
        affix, dictionary = root / 'український.aff', root / 'український.dic'
        affix.write_text('SET UTF-8\nFLAG UTF-8\nTRY abcdefghijklmnopqrstuvwxyz\n'
                         'SFX S Y 1\nSFX S 0 s .\n', encoding='utf-8')
        dictionary.write_text('5\nhello\nworld/S\nretained\nслово\nУкраїна\n', encoding='utf-8')
        original = affix.read_bytes(), dictionary.read_bytes()
        profile = review(library, affix, dictionary, language='synthetic en+uk',
            provenance='Original acceptance dictionaries only. Explicit external native library; no redistribution approval.')
        check('exact selected native library hash', profile.library.sha256 == sha256(Path(library).read_bytes()).hexdigest())
        text = '😀 hello\r\nwrld worlds Україна слово'
        result = run(text, profile)
        check('actual C API, UTF-8 dictionary and affix expansion', result.words == 5 and result.encoding == 'UTF-8'
              and len(result.issues) == 1 and result.issues[0].word == 'wrld'
              and 'world' in result.issues[0].suggestions)
        check('exact Unicode/CRLF reading offsets', (result.issues[0].start, result.issues[0].end) == (9, 13))
        selected = run(text, profile, bounds=(text.index('Україна'), len(text)))
        check('selected-text scope with original character positions', selected.words == 2 and not selected.issues)
        long_word = run('a' * 129, profile)
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
        legacy = review(library, legacy_aff, legacy_dic, language='synthetic legacy',
                                          provenance='Original two-word ISO8859-1 fixture')
        legacy_result = run('café Україна', legacy)
        check('declared legacy encoding with no replacement decoding', len(legacy_result.issues) == 1
              and legacy_result.issues[0].word == 'Україна' and legacy_result.issues[0].reason == 'unencodable')
        dictionary.write_bytes(original[1].replace(b'hello', b'other'))
        try:
            run('hello', profile)
        except ValueError as exc:
            check('same-size changed dictionary is rejected before use', 'changed' in str(exc))
        else:
            raise AssertionError('Changed dictionary was accepted.')
        dictionary.write_bytes(original[1])
        affix.write_bytes(b'This is not a Hunspell affix declaration.\n')
        invalid = review(library, affix, dictionary, language='synthetic invalid',
                                           provenance='Deliberately malformed original test fixture')
        try:
            run('hello', invalid)
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
        large_profile = review(library, affix, large, language='synthetic lifecycle',
                                                provenance='Original generated lifecycle fixture')
        actual_run = spelling.run_process
        observed, children = [], []
        deadline_fixture = [False]
        from carr_library.audio import process as process_module
        if os.name == 'nt':
            from carr_library.audio import windows_job
            actual_child = windows_job.JobProcess
            def create_child(*args, **options):
                child = actual_child(*args, **options); children.append(child)
                return child
            windows_job.JobProcess = create_child
            def restore_child_factory():
                windows_job.JobProcess = actual_child
        else:
            from types import SimpleNamespace
            actual_subprocess = process_module.subprocess
            def create_child(*args, **options):
                child = actual_subprocess.Popen(*args, **options); children.append(child)
                return child
            replacement = SimpleNamespace(**vars(actual_subprocess))
            replacement.Popen = create_child
            process_module.subprocess = replacement
            def restore_child_factory():
                process_module.subprocess = actual_subprocess
        def dead_children():
            # Recorded before any fallback; current Windows close also confirms
            # the private Job is empty before releasing its native handle.
            return bool(children) and all(child.poll() is not None
                and (os.name != 'nt' or child._job is None) for child in children)

        def observe(command, **kwargs):
            observed.append(Path(kwargs['cwd']))
            if deadline_fixture[0]:
                # The C API already ran above. This separate deadline case
                # delays the same unchanged standalone worker in an actual
                # owned stdlib child, so machine startup is not a 50 ms race.
                command = [command[0], '-I', '-B', '-S', '-c',
                    'import runpy,sys,time; sys.argv=sys.argv[1:]; time.sleep(45); '
                    'runpy.run_path(sys.argv[0],run_name="__main__")', *command[4:]]
            return actual_run(command, **kwargs)
        spelling.run_process = observe
        try:
            try:
                run('hello', large_profile,
                    cancelled=lambda: bool(observed and (observed[-1] / 'dictionary.dic').exists()))
            except InterruptedError:
                check('actual mid-staging/check cancellation reaps and removes private copies',
                      bool(observed) and dead_children() and not observed[-1].exists())
            else:
                raise AssertionError('The active native worker did not observe cancellation.')
            deadline_fixture[0] = True
            try:
                run('hello', large_profile, timeout=3)
            except TimeoutError:
                check('actual owned deadline wrapper reaps and removes private copies', len(observed) == 2 and len(children) == 2
                      and dead_children() and not observed[-1].exists())
            else:
                raise AssertionError('The active native worker did not reach its deadline.')
        finally:
            spelling.run_process = actual_run
            restore_child_factory()
            for child in children:
                if child.poll() is None:
                    child.kill(); child.wait(timeout=5)
                if os.name == 'nt':
                    child.close()
        from carr_library.operations import ResourceBroker
        check('shared spelling reservations released after native cleanup',
              ResourceBroker.for_app(state_root.parent).snapshot()['leases'] == [])
        check('native engine bytes remain unchanged', profile.library.sha256 == sha256(Path(library).read_bytes()).hexdigest())

    check('installed sources unchanged after actual C API acceptance',
          checked_origins() == origins_before)
    return {'status': 'PASS', 'checks': checks, 'native_library_sha256': profile.library.sha256,
            'native_library_bytes': profile.library.size, 'application_modules': origins_before,
            'actual_c_api': 'PASS', 'child_and_resource_cleanup': 'PASS',
            'deadline_fixture': 'Owned stdlib delay before the unchanged standalone worker; actual C API tested separately',
            'dictionaries': 'Original synthetic fixtures; no language-quality qualification',
            'native_windows': 'EXECUTED' if os.name == 'nt' else 'NOT_RUN',
            'manual_nvda': 'NOT_RUN', 'distribution_clearance': 'NOT_GRANTED'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', required=True, help='Explicit reviewed standalone Hunspell native library')
    parser.add_argument('--output', help='New JSON report path; an existing report is never replaced')
    parser.add_argument('--source-root', required=True, help='Exact checked-out carr-library/src')
    parser.add_argument('--require-installed', action='store_true')
    args = parser.parse_args()
    report = accept(args.library, source_root=args.source_root, require_installed=args.require_installed)
    payload = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        from carr_library.speech._io import new_output
        with new_output(args.output) as staged:
            staged.write_text(payload, encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
