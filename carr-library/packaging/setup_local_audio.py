"""Explicit local audio setup using the existing pinned acquisition/preparation.

Default: show a plan without writing or downloading. --interactive requires YES;
--download is explicit non-interactive consent. The prepared runtime stays outside
the installation and is selected through the existing engine settings dialog.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import os
from pathlib import Path
import struct
import sys
from uuid import uuid4

from carr_library import audio_runtime
from carr_library.durable_state import local_path, regular

HERE = Path(__file__).absolute().parent


def _installation_root():
    # Both the shipped source-only tools layout and a source checkout are supported.
    if HERE.name == 'audio-setup' and HERE.parent.name == 'tools':
        return local_path(HERE.parent.parent, exists=True)
    return local_path(HERE.parent, exists=True)


def _load_helper(name):
    """Load only a fixed sibling helper without creating installation bytecode."""
    if name not in {'acquire_audio_runtime', 'prepare_audio_runtime'}:
        raise ValueError('Unknown audio setup helper.')
    path = local_path(HERE / (name + '.py'), exists=True)
    regular(path)
    spec = importlib.util.spec_from_file_location('carr_local_audio_' + name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError('Audio setup helper cannot be loaded.')
    module = importlib.util.module_from_spec(spec)
    previous_path = list(sys.path)
    previous_bytecode = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    finally:
        # The unchanged developer helpers add their source-checkout import path.
        # The installed setup uses the venv package and does not retain that path.
        sys.path[:] = previous_path
        sys.dont_write_bytecode = previous_bytecode
    return module


def _default_output():
    base = os.environ.get('LOCALAPPDATA')
    if not base:
        raise ValueError('Не знайдено LOCALAPPDATA. Вкажіть нову локальну папку через --output.')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    return Path(base) / 'Resonance' / 'runtimes' / ('audio-' + stamp + '-' + uuid4().hex[:8])


def _external_output(value):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Вкажіть абсолютний локальний шлях без переходів «..».')
    path = local_path(path)
    installed = _installation_root()
    if path.is_relative_to(installed) or installed.is_relative_to(path):
        raise ValueError('Папка аудіокомпонентів має бути поза папкою встановленої програми.')
    if os.path.lexists(path):
        raise FileExistsError('Папка вже існує; виберіть нову папку: ' + str(path))
    return path


def _require_windows():
    if sys.platform != 'win32' or struct.calcsize('P') != 8:
        raise ValueError('Ці локальні аудіокомпоненти призначені для Windows x64.')


def _show_distribution_limits():
    # The pinned acquisition/verification contract admits only the owner-local profile.
    print('Лише локальне використання власником. Право на публічне розповсюдження пакета не підтверджено; комплект вихідних кодів сторонніх залежностей неповний.')


def _show_plan(records, output):
    print('Локальне налаштування аудіо Резонанс')
    print('Буде завантажено три зафіксовані публічні файли FFmpeg та його вихідних матеріалів.')
    print('Розмір завантаження: ' + str(sum(item['size'] for item in records)) + ' байт.')
    for item in records:
        print('  ' + item['name'] + ' — ' + str(item['size']) + ' байт')
    print('Нова папка: ' + str(output))
    print('Програма перевірить розміри й SHA-256 за наявним source-lock.')
    print('Документи, голоси, ключі API й налаштування бібліотеки не надсилаються.')
    print('Налаштування двигунів не змінюються автоматично.')
    _show_distribution_limits()


def _prepare(output, records, acquire, prepare):
    """Download and prepare only after the command has obtained explicit consent."""
    _require_windows()
    output = _external_output(output)  # Recheck after the confirmation prompt.
    if acquire.inputs() != records:
        raise ValueError('План завантаження змінився; запустіть налаштування знову.')
    output.parent.mkdir(parents=True, exist_ok=True)
    local_path(output.parent, exists=True)
    output.mkdir(mode=0o700, exist_ok=False)
    local_path(output, exists=True)
    inputs = output / 'inputs'
    inputs.mkdir(mode=0o700)
    for record in records:
        print('Отримання: ' + record['name'], flush=True)
        acquire.download_one(record, inputs)
    runtime = output / 'runtime'
    print('Перевірка та підготовка локальних файлів...', flush=True)
    prepare.prepare(inputs / records[0]['name'], inputs, runtime)
    # Verify the published directory, not only the preparer's temporary snapshot.
    report = audio_runtime.verify_audio_runtime(runtime)
    return report


def _show_result(report):
    print('Підготовку завершено. Файли перевірено; аудіопрограми не запускалися.')
    _show_distribution_limits()
    print('Відкрийте в Резонанс «Local audio, voice and music engines».')
    print('Натисніть «Choose FFmpeg executable» і виберіть:')
    print(report['tools']['ffmpeg'])
    print('Натисніть «Choose FFprobe executable» і виберіть:')
    print(report['tools']['ffprobe'])
    print('Ці шляхи застосовуються до поточної бібліотеки. Потім повторіть потрібну аудіодію.')
    print('Зберігайте папку runtime разом із її бібліотеками, повідомленнями про ліцензії та джерелами.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='New local work folder outside the installation.')
    consent = parser.add_mutually_exclusive_group()
    consent.add_argument('--interactive', action='store_true', help='Ask for YES before any download or write.')
    consent.add_argument('--download', action='store_true', help='Explicitly download and prepare the pinned inputs.')
    args = parser.parse_args(argv)
    try:
        acquire = _load_helper('acquire_audio_runtime')
        prepare = _load_helper('prepare_audio_runtime')
        records = acquire.inputs()
        output = args.output if args.output is not None else _default_output()
        if args.interactive and args.output is None:
            print('Запропонована нова папка: ' + str(output))
            selected = input('Інша нова папка (або Enter для запропонованої): ').strip()
            if selected:
                output = Path(selected)
        output = _external_output(output)
        _show_plan(records, output)
        if not args.download:
            if not args.interactive:
                print('Лише план: нічого не завантажено й не записано.')
                print('Для запуску скористайтеся --interactive або явним --download.')
                return 0
            if input('Дозволити завантаження та створення цієї папки? Введіть YES: ').strip() != 'YES':
                print('Скасовано: нічого не завантажено й не записано.')
                return 0
        report = _prepare(output, records, acquire, prepare)
        _show_result(report)
        return 0
    except EOFError:
        print('Скасовано: підтвердження не отримано.')
        return 0
    except KeyboardInterrupt:
        print('\nПерервано. Налаштування двигунів не змінено; перевірте створену папку перед повторенням.')
        return 130
    except Exception as exc:
        print('Не вдалося завершити підготовку: ' + type(exc).__name__ + ': ' + str(exc), file=sys.stderr)
        print('Налаштування двигунів не змінено. Якщо папку вже створено, збережіть її для перевірки '
              'та виберіть іншу нову папку для повторення.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

