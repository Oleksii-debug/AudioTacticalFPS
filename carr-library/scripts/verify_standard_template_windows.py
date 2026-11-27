"""Fast actual Inno compilation and native character-predicate qualification.

Compile both exact complete templates with inert placeholder file entries and
output disabled. Separately execute only the two copied validation predicates
inside a disposable no-files/no-uninstaller fixture. This is not installation,
repair, rollback, signed-update or full-bundle qualification.
"""
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


def digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def ordinary(path):
    path = Path(path)
    info = path.lstat()
    if (not path.is_absolute() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, 'st_file_attributes', 0) & 0x400):
        raise ValueError('Compiler inputs must be explicit ordinary local files.')
    return path.resolve(strict=True)


def compiler_path(value):
    if value:
        selected = Path(value)
    else:
        candidates = [Path(os.environ[key]) / 'Inno Setup 6' / 'ISCC.exe'
                      for key in ('ProgramFiles(x86)', 'ProgramFiles') if os.environ.get(key)]
        selected = next((path for path in candidates if path.is_file()), None)
    if selected is None or selected.name.casefold() != 'iscc.exe':
        raise ValueError('An already-installed Inno compiler is required at an explicit reviewed path.')
    return ordinary(selected)


def git(project, *args):
    return subprocess.run(['git', *args], cwd=project, check=True, capture_output=True,
                          text=True, timeout=20).stdout.strip()


def run(executable, arguments, directory, log, *, timeout):
    with Path(log).open('wb') as stream:
        process = subprocess.run([str(executable), *map(str, arguments)], cwd=directory,
            stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT, timeout=timeout, check=False)
    text = Path(log).read_text(encoding='utf-8', errors='replace')
    if process.returncode:
        print(text[-48000:], flush=True)
        raise ValueError('Actual native command failed with exit ' + str(process.returncode) + ': ' + Path(log).name)
    return text


def predicates(template):
    first = template.index('function LowerHex(')
    middle = template.index('\nfunction PreviousGeneration(', first)
    last = template.index('\nprocedure CheckPendingUpdate;', middle)
    result = template[first:middle] + '\n' + template[middle + 1:last]
    if result.count('function LowerHex(') != 1 or result.count('function PreviousGeneration(') != 1:
        raise ValueError('The exact predicate extraction is ambiguous.')
    return result


def full_compile(project, source, compiler, stage, evidence, label):
    if git(project, 'rev-parse', 'HEAD') != source:
        raise ValueError('Template checkout differs from its declared source.')
    git(project, 'diff', '--exit-code', 'HEAD', '--')
    builder_path = project / 'packaging/build_standard_installer.py'
    spec = importlib.util.spec_from_file_location('_inno_template_' + label, builder_path)
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    stage.mkdir()
    payload = stage / 'payload'
    payload.mkdir()
    files = {}
    # This full-template compile fixture is inert. It is never installed or
    # represented as a transferable sealed application.
    for name in ('README.txt', 'ResearchLibrary.exe', 'runtime/python.exe', 'Setup.cmd', 'install_windows.py'):
        path = payload / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'INERT COMPILER FIXTURE; NEVER EXECUTE AS APPLICATION\n')
        files[name] = digest(path)
    manifest = b'{"schema":"inert-template-compilation-only"}\n'
    (payload / 'bundle-manifest.json').write_bytes(manifest)
    (stage / 'install-layout-v1.txt').write_text('Resonance.StandardInstall/1\n', encoding='utf-8')
    recipe = builder.recipe(files, source, sha256(manifest).hexdigest())
    if any(token in recipe for token in ('@SOURCE@', '@MANIFEST@', '@PAYLOAD_', '@TEMP_FILES@')):
        raise ValueError('The full standard template retained an unresolved placeholder.')
    script = '#if Ver < 0x06040000\n#error Inno Setup 6.4 or later is required.\n#endif\n' + recipe
    path = stage / 'Resonance.iss'
    path.write_text(script, encoding='utf-8-sig')
    log = evidence / ('full-template-' + label + '.txt')
    transcript = run(compiler, ['/O-', path], stage, log, timeout=90)
    versions = re.findall(r'^Compiler engine version: Inno Setup ([0-9]+\.[0-9]+\.[0-9]+(?:\.[0-9]+)?)(?: \([^\r\n]*\))?\s*$',
                          transcript, flags=re.M)
    if len(versions) != 1 or tuple(map(int, versions[0].split('.'))) < (6, 4, 0):
        raise ValueError('The actual full-template compiler did not identify one acceptable engine version.')
    return dict(source_commit=source, source_tree=git(project, 'rev-parse', 'HEAD^{tree}'),
        template_sha256=digest(project / 'packaging/standard_windows.iss.in'),
        builder_sha256=digest(builder_path), recipe_sha256=digest(path), compiler_engine_version=versions[0],
        output_disabled=True, full_template_compiled=True, payload_kind='inert compile fixtures only')


def pascal_quote(value):
    return "'" + value.replace("'", "''") + "'"


def predicate_script(functions):
    token = '0123456789abcdef0123456789abcdef'
    previous = 'versions/0123456789ab-'
    examples = [
        ('LowerHex(' + pascal_quote(token) + ', 32)', True),
        ('LowerHex(' + pascal_quote(token) + ', 31)', False),
        ('LowerHex(' + pascal_quote(token) + ', 33)', False),
        ('LowerHex(' + pascal_quote(token.upper()) + ', 32)', False),
        ("LowerHex('', 32)", False),
        ('LowerHex(' + pascal_quote(token + token) + ', 64)', True),
        ('LowerHex(' + pascal_quote((token + token).upper()) + ', 64)', False),
        ('PreviousGeneration(' + pascal_quote(previous + '1') + ')', True),
        ('PreviousGeneration(' + pascal_quote(previous + '0') + ')', False),
        ('PreviousGeneration(' + pascal_quote(previous + '01') + ')', False),
        ("PreviousGeneration('versions/0123456789AB-1')", False),
        ("PreviousGeneration('Versions/0123456789ab-1')", False),
        ('PreviousGeneration(' + pascal_quote(previous + '1000000') + ')', False),
        ('PreviousGeneration(' + pascal_quote(previous + '1/') + ')', False),
        ('PreviousGeneration(' + pascal_quote(previous + '-1') + ')', False),
        ("PreviousGeneration('../versions/0123456789ab-1')", False),
    ]
    count = 128 * 2 + 8 + len(examples)
    assertions = '\n'.join('  Check((' + expression + ') = ' + str(expected) + ', ' +
                           pascal_quote('fixed-' + str(index)) + ');'
                           for index, (expression, expected) in enumerate(examples, 1))
    header = r"""#if Ver < 0x06040000
#error Inno Setup 6.4 or later is required.
#endif
[Setup]
AppId=CARR.InnoPredicateFixture
AppName=CARR native predicate fixture
AppVersion=1
CreateAppDir=no
Uninstallable=no
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=predicate-fixture
SetupLogging=yes
[Code]
var
  PredicateCount: Integer;
"""
    body = r"""
procedure Check(Passed: Boolean; Name: String);
begin
  PredicateCount := PredicateCount + 1;
  if not Passed then RaiseException('Predicate failed: ' + Name);
end;

function InitializeSetup(): Boolean;
var
  I: Integer;
  Value: String;
begin
  PredicateCount := 0;
  for I := 0 to 127 do begin
    Check(LowerHex(Chr(I), 1) =
      (((I >= 48) and (I <= 57)) or ((I >= 97) and (I <= 102))),
      'lower-hex-character-' + IntToStr(I));
    Check(PreviousGeneration('versions/0123456789ab-' + Chr(I)) =
      ((I >= 49) and (I <= 57)), 'generation-character-' + IntToStr(I));
  end;
  Value := '1';
  for I := 1 to 8 do begin
    Check(PreviousGeneration('versions/0123456789ab-' + Value) = (I <= 6),
      'generation-length-' + IntToStr(I));
    Value := Value + '0';
  end;
@ASSERTIONS@
  Check(PredicateCount = @COUNT@, 'exact predicate inventory');
  Result := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Output: String;
begin
  if CurStep = ssDone then begin
    Output := ExpandConstant('{param:CARRPROOF|}');
    if Output = '' then RaiseException('No explicit predicate proof output.');
    if not SaveStringToFile(Output,
        '{"schema":1,"status":"PASS","predicate_cases":@TOTAL@,"native_installation":"NOT_RUN"}',
        False) then RaiseException('Could not write the native predicate proof.');
  end;
end;
"""
    # The inventory assertion itself also executes and must be counted.
    body = body.replace('@ASSERTIONS@', assertions).replace('@COUNT@', str(count)).replace('@TOTAL@', str(count + 1))
    return header + functions + body, count + 1


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--current', type=Path, required=True)
    parser.add_argument('--previous', type=Path, required=True)
    parser.add_argument('--current-source', required=True)
    parser.add_argument('--previous-source', required=True)
    parser.add_argument('--compiler', default=os.environ.get('CARR_INNO_COMPILER'))
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    evidence = args.output.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    report = dict(schema=1, status='RUNNING', full_templates='NOT_RUN', native_predicates='NOT_RUN',
                  native_installation='NOT_RUN', standard_update_AB15='NOT_RUN', standard_installation50='NOT_RUN')
    try:
        if sys.platform != 'win32' or not sys.flags.isolated:
            raise ValueError('Actual isolated Windows is required for this native gate.')
        current, previous = args.current.resolve(strict=True), args.previous.resolve(strict=True)
        compiler = compiler_path(args.compiler)
        engine = ordinary(compiler.with_name('ISCmplr.dll'))
        identities = {str(path): digest(path) for path in (compiler, engine)}
        report.update(compiler_path=str(compiler), compiler_sha256=digest(compiler),
                      compiler_engine_sha256=digest(engine),
                      harness_sha256=digest(Path(__file__)))
        work = args.work.resolve()
        work.mkdir(parents=True, exist_ok=False)
        sources = {}
        for label, project, source in (('A', previous, args.previous_source), ('B', current, args.current_source)):
            text = (project / 'packaging/standard_windows.iss.in').read_text('utf-8')
            sources[label] = text
            report[label] = full_compile(project, source, compiler, work / label, evidence, label)
        if sources['A'] != sources['B']:
            raise ValueError('A and B did not incorporate byte-identical reviewed template corrections.')
        functions = predicates(sources['B'])
        script, expected = predicate_script(functions)
        fixture = work / 'predicates'
        fixture.mkdir()
        (fixture / 'predicates.iss').write_text(script, encoding='utf-8-sig')
        run(compiler, [fixture / 'predicates.iss'], fixture, evidence / 'predicate-compile.txt', timeout=90)
        executable = ordinary(fixture / 'dist/predicate-fixture.exe')
        proof = fixture / 'predicate-result.json'
        executable_sha = digest(executable)
        run(executable, ['/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-',
                         '/CARRPROOF=' + str(proof), '/LOG=' + str(evidence / 'predicate-native.log')],
            fixture, evidence / 'predicate-process.txt', timeout=60)
        value = json.loads(proof.read_text(encoding='utf-8-sig'))
        if value != dict(schema=1, status='PASS', predicate_cases=expected, native_installation='NOT_RUN'):
            raise ValueError('Actual predicate execution did not complete its exact required inventory.')
        if digest(executable) != executable_sha or identities != {str(path): digest(path) for path in (compiler, engine)}:
            raise ValueError('Native compiler or predicate fixture changed during execution.')
        (evidence / 'predicate-result.json').write_text(json.dumps(value, sort_keys=True) + '\n', encoding='utf-8')
        report.update(full_templates=2, native_predicates='PASS', predicate_cases=expected,
                      predicates_sha256=sha256(functions.encode('utf-8')).hexdigest(),
                      predicate_script_sha256=digest(fixture / 'predicates.iss'),
                      predicate_executable_sha256=executable_sha)
        for label, project in (('A', previous), ('B', current)):
            if digest(project / 'packaging/standard_windows.iss.in') != report[label]['template_sha256']:
                raise ValueError('A source template changed during the native check.')
            git(project, 'diff', '--exit-code', 'HEAD', '--')
        report.update(status='PASS', source_templates_unchanged=True)
    except BaseException as error:
        report.update(status='FAIL', error_type=type(error).__name__, error=str(error)[:2000])
        raise
    finally:
        (evidence / 'template-gate.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print(json.dumps(report, sort_keys=True))


if __name__ == '__main__':
    main()
