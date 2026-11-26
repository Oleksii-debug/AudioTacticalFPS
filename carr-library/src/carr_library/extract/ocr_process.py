"""Execute the verified OCR engine without Windows narrow-argv path loss.

Python supplies the child working directory through CreateProcessW. Relative
ASCII paths therefore reach native builds that cannot represent a Ukrainian
absolute path in argv. Only when that is insufficient do we copy bounded,
stable inputs/models to a private temporary directory. No short-name, shell,
global cwd/environment, executable replacement, or model fallback is used.
"""
from hashlib import sha256
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile

from .ocr_languages import model_ids
from .ocr_runtime import OcrRuntimeError

_MAX_MODEL = 256 * 1024 * 1024
_MAX_MODELS = 512 * 1024 * 1024
_MAX_IMAGE = 128 * 1024 * 1024
_MAX_OUTPUT = 16 * 1024 * 1024
_INFORMATIONAL = frozenset(('--version', '--list-langs', '--help', '--help-extra',
                            '--help-psm', '--help-oem', '--print-parameters'))


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _regular(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, 'st_file_attributes', 0) & 0x400):
        raise OcrRuntimeError('OCR staging requires regular, unlinked files.')
    return info


def _directory(path):
    path = Path(path).absolute()
    for item in (path, *path.parents):
        info = item.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise OcrRuntimeError('OCR staging folders cannot be links or reparse points.')
    return path


def _copy(source, target, limit, remaining, expected=None):
    """Stream stable bytes, then read back the private copy before native use."""
    _directory(source.parent)
    before = _regular(source)
    if before.st_size > limit:
        raise OcrRuntimeError('OCR staging file exceeds its size limit.')
    created = False
    try:
        digest = sha256()
        count = 0
        with source.open('rb') as inp, target.open('xb') as out:
            created = True
            if _stamp(os.fstat(inp.fileno())) != _stamp(before):
                raise OcrRuntimeError('OCR staging input changed while opening.')
            while chunk := inp.read(1024 * 1024):
                remaining()
                count += len(chunk)
                if count > limit:
                    raise OcrRuntimeError('OCR staging input grew past its size limit.')
                digest.update(chunk)
                out.write(chunk)
            if _stamp(os.fstat(inp.fileno())) != _stamp(before):
                raise OcrRuntimeError('OCR staging input changed while copying.')
        if count != before.st_size or _stamp(_regular(source)) != _stamp(before):
            raise OcrRuntimeError('OCR staging input changed during copying.')
        value = digest.hexdigest()
        if expected is not None and value != expected:
            raise OcrRuntimeError('OCR staging model differs from the selected bytes.')
        saved = sha256()
        with target.open('rb') as stream:
            while chunk := stream.read(1024 * 1024):
                remaining()
                saved.update(chunk)
        if _regular(target).st_size != count or saved.hexdigest() != value:
            raise OcrRuntimeError('OCR staging copy could not be verified.')
        remaining()
        return count

    except BaseException:
        if created:
            target.unlink(missing_ok=True)
        raise


def _option(command, name):
    found = []
    for index, arg in enumerate(command[1:], 1):
        if arg == name:
            if index + 1 >= len(command):
                raise OcrRuntimeError('An OCR option has no value.')
            found.append((index, index + 1, command[index + 1]))
        elif arg.startswith(name + '='):
            found.append((index, index, arg[len(name) + 1:]))
    if len(found) > 1:
        raise OcrRuntimeError('An OCR option is repeated.')
    return found[0] if found else None


def _replace_option(command, option, name, value):
    result = list(command)
    index, value_index, _ = option
    result[value_index] = value if index != value_index else name + '=' + value
    return result


def _positions(command):
    index = 1
    paired = {'--tessdata-dir', '--oem', '--psm', '--dpi', '-l', '-c'}
    while index < len(command) and command[index].startswith('-'):
        if command[index] in paired:
            index += 2
        elif command[index].startswith(('--tessdata-dir=', '--oem=', '--psm=', '--dpi=')):
            index += 1
        else:
            raise OcrRuntimeError('Unsupported OCR invocation option.')
    if index + 1 >= len(command):
        raise OcrRuntimeError('An OCR invocation needs an input and an output.')
    return index, index + 1


def _relative(path, cwd):
    try:
        value = os.path.relpath(path, cwd)
    except ValueError:  # Different Windows drives.
        return None
    return value if value.isascii() else None


def _execute(command, cwd, remaining):
    return subprocess.run(command, check=True, capture_output=True,
                          timeout=remaining(), cwd=cwd,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def normalize_ocr_query_output(data, directory):
    """Map only the real engine's validated relative header to its source."""
    if directory is None:
        return data
    cwd = directory
    report = data.decode('utf-8')
    match = re.search(r'^List of available languages in "([^"\r\n]+)"', report, re.MULTILINE)
    if match is None:
        raise OcrRuntimeError('The OCR engine did not report its model directory.')
    reported = Path(match[1])
    resolved = reported if reported.is_absolute() else cwd / reported
    try:
        same = resolved.samefile(directory)
    except OSError:
        same = False
    if not same:
        raise OcrRuntimeError('The OCR engine reported a different model directory.')
    report = report[:match.start(1)] + str(directory) + report[match.end(1):]
    return report.encode('utf-8')


def prepare_ocr_query(runtime, argv):
    """Prepare informational argv/cwd while callers retain bounded Popen IO."""
    command = runtime.command(argv)
    if (sys.platform != 'win32' or not _INFORMATIONAL.intersection(command)
            or all(arg.isascii() for arg in command[1:])):
        return command, runtime.root, None
    option = _option(command, '--tessdata-dir')
    if option is None:
        return command, runtime.root, None
    directory = _directory(Path(option[2]))
    bound = _replace_option(command, option, '--tessdata-dir', '.')
    if not all(arg.isascii() for arg in bound[1:]):
        raise OcrRuntimeError('Unsupported Unicode OCR option.')
    return bound, directory, directory if '--list-langs' in command else None


def _model_expectations(runtime, directory):
    if getattr(runtime, 'managed', False) and directory == Path(runtime.tessdata).absolute():
        return dict(runtime.selection.model_sha256)
    if runtime.portable and directory == Path(runtime.tessdata).absolute():
        prefix = Path(runtime.tessdata).relative_to(runtime.root).as_posix() + '/'
        return {name[len(prefix):-len('.traineddata')]: value
                for name, value in runtime.files_sha256
                if name.startswith(prefix) and name.endswith('.traineddata')}
    return {}


def run_ocr(runtime, argv, *, remaining):
    """Run one CARR OCR call, retaining the caller's deadline and exact engine.

    command() remains a pure binding API. Executing code uses this helper so
    native Windows builds see ASCII file arguments even under Unicode profiles.
    Original image/model files are never renamed, linked, or overwritten.
    """
    command = runtime.command(argv)
    if sys.platform != 'win32' or all(arg.isascii() for arg in command[1:]):
        return _execute(command, runtime.root, remaining)
    model_option = _option(command, '--tessdata-dir')
    model_directory = _directory(Path(model_option[2])) if model_option else None
    if _INFORMATIONAL.intersection(command):
        bound, cwd, report_directory = prepare_ocr_query(runtime, argv)
        result = _execute(bound, cwd, remaining)
        return subprocess.CompletedProcess(result.args, result.returncode,
            normalize_ocr_query_output(result.stdout, report_directory), result.stderr)

    input_index, output_index = _positions(command)
    base = Path(runtime.root or Path.cwd()).absolute()
    source = base / Path(command[input_index])
    stdout = command[output_index] in ('stdout', '-')
    destination = None if stdout else base / Path(command[output_index])
    cwd = model_directory if model_directory is not None else Path(runtime.root or Path.cwd())
    relative_input = _relative(source, cwd)
    relative_output = 'stdout' if stdout else _relative(destination, cwd)
    if relative_input is not None and relative_output is not None:
        bound = (_replace_option(command, model_option, '--tessdata-dir', '.')
                 if model_option else list(command))
        bound[input_index], bound[output_index] = relative_input, relative_output
        if all(arg.isascii() for arg in bound[1:]):
            return _execute(bound, cwd, remaining)

    with tempfile.TemporaryDirectory(prefix='carr-ocr-paths-') as temporary:
        staging = _directory(Path(temporary))
        if shutil.disk_usage(staging).free < _regular(source).st_size + 32 * 1024 * 1024:
            raise OcrRuntimeError('Not enough free space for OCR staging.')
        image = staging / 'input.png'
        _copy(source, image, _MAX_IMAGE, remaining)
        output = staging / 'result'
        relative_input = _relative(image, cwd)
        relative_output = 'stdout' if stdout else _relative(output, cwd)
        bound = list(command)
        if model_option:
            bound = _replace_option(bound, model_option, '--tessdata-dir', '.')
        if relative_input is None or relative_output is None:
            cwd = staging
            relative_input, relative_output = 'input.png', 'stdout' if stdout else 'result'
            if model_directory is not None:
                model_relative = _relative(model_directory, cwd)
                if model_relative is None:
                    language_option = _option(command, '-l')
                    languages = model_ids(language_option[2] if language_option else 'eng')
                    expectations = _model_expectations(runtime, model_directory)
                    model_stage = staging / 'models'
                    model_stage.mkdir(mode=0o700)
                    total = 0
                    for language in dict.fromkeys(languages):
                        original = model_directory / (language + '.traineddata')
                        if expectations and language not in expectations:
                            raise OcrRuntimeError('OCR staging requested an unselected model.')
                        size = _regular(original).st_size
                        total += size
                        if (total > _MAX_MODELS or size > _MAX_MODEL
                                or shutil.disk_usage(staging).free < size + 32 * 1024 * 1024):
                            raise OcrRuntimeError('OCR model staging exceeds its storage limit.')
                        target = model_stage / (language + '.traineddata')
                        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        _copy(original, target, _MAX_MODEL, remaining, expectations.get(language))
                    model_relative = 'models'
                bound = _replace_option(bound, model_option, '--tessdata-dir', model_relative)
        bound[input_index], bound[output_index] = relative_input, relative_output
        if not all(arg.isascii() for arg in bound[1:]):
            raise OcrRuntimeError('Unsupported Unicode OCR option.')
        result = _execute(bound, cwd, remaining)
        if destination is not None:
            products = list(staging.glob('result.*'))
            if not products or any(p.suffix not in ('.txt', '.tsv', '.osd') for p in products):
                raise OcrRuntimeError('The OCR engine returned unexpected output files.')
            copied = []
            try:
                for product in products:
                    target = Path(str(destination) + product.suffix)
                    _directory(target.parent)
                    _copy(product, target, _MAX_OUTPUT, remaining)
                    copied.append(target)
            except BaseException:
                for target in copied:
                    target.unlink(missing_ok=True)
                raise
        remaining()
        return result
