"""Fixed calibre interpreter payload copied into one private conversion stage.

This file is executed only by the explicitly selected calibre-debug companion.
It is not a user plugin, recipe, Python template, external filter or a general
script interface. calibre's public conversion CLI supplies the existing codecs.
"""
import os
from pathlib import Path
import sys


def _owned_temp_cleanup(module, stage):
    """Keep calibre's persistent temp cleanup with the existing stage owner.

    The pinned helper's two deferred-deletion registrations need no separate
    program: the enclosing conversion worker already removes its whole private
    stage after native settlement, including forced cancellation. Program
    launch registrations and paths outside that stage remain forbidden.
    """
    if (module.worker is not None or module.RMTREE_ACTION != 'rmtree'
            or module.UNLINK_ACTION != 'unlink' or module.RUN_PROGRAM_ACTION != 'run_program'
            or not callable(module._send_command)):
        raise ValueError('Unsupported calibre temporary-cleanup protocol.')

    def register(action, payload):
        if action not in ('rmtree', 'unlink'):
            raise PermissionError('Additional programs are disabled in this reading conversion.')
        if type(payload) is not str or not 0 < len(payload) <= 32768 or '\0' in payload:
            raise PermissionError('Invalid calibre temporary-cleanup path.')
        path = Path(payload)
        if not path.is_absolute() or '..' in path.parts:
            raise PermissionError('Calibre cleanup requires an owned temporary path.')
        path = path.resolve()
        if path == stage or not path.is_relative_to(stage):
            raise PermissionError('Calibre cleanup cannot own an unselected path.')
        # No unlink/rmtree, atexit task, helper process or retained registration
        # is created here. This file remains under the enclosing stage's owner.

    module._send_command = register


def main():
    if len(sys.argv) != 5:
        raise ValueError('Invalid fixed calibre conversion arguments.')
    source, target, root = (Path(value).resolve(strict=True) for value in sys.argv[1:4])
    maximum = int(sys.argv[4])
    stage = source.parent
    if (target != stage / 'calibre.txt' or target.stat().st_size != 0
            or source.name not in {'source' + suffix for suffix in ('.azw', '.azw3', '.lit', '.mobi', '.prc', '.tcr')}
            or not 1 <= maximum <= 32 * 1024**2
            or Path(__file__).resolve().parent != stage):
        raise ValueError('Invalid fixed calibre source/output boundary.')
    # Imports and startup use a fresh configuration owned by this operation.
    # No existing plugin configuration, custom recipe or template is inherited.
    if Path(os.environ['CALIBRE_CONFIG_DIRECTORY']).resolve() != stage / 'calibre-config':
        raise ValueError('Calibre did not receive its private empty configuration.')
    sys.dont_write_bytecode = True
    system = Path(os.environ.get('SYSTEMROOT', '/nonexistent-carr-system')).resolve()
    roots = (stage, root, system)
    mutation_flags = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC

    def inside(value, allowed):
        if isinstance(value, int):
            return True
        path = Path(os.fsdecode(value)).resolve()
        return any(path == parent or path.is_relative_to(parent) for parent in allowed)

    def audit(event, values):
        if (event.startswith('socket.') or event in
                ('subprocess.Popen', 'os.system', 'os.posix_spawn', 'os.posix_spawnp')
                or event.startswith('os.exec') or event.startswith('os.spawn')):
            raise PermissionError('Network and additional programs are disabled in this reading conversion.')
        if event == 'open' and not isinstance(values[0], int):
            mode = values[1]
            writing = bool((isinstance(mode, str) and any(flag in mode for flag in 'wax+'))
                           or isinstance(values[2], int) and values[2] & mutation_flags)
            if os.fsdecode(values[0]) != os.devnull and not inside(values[0], (stage,) if writing else roots):
                raise PermissionError('Calibre cannot read or change an unselected document path.')
        if event in ('os.link', 'os.symlink'):
            raise PermissionError('Linked output paths are disabled in this reading conversion.')
        if event in ('os.rename', 'os.replace'):
            if not all(inside(value, (stage,)) for value in values[:2]):
                raise PermissionError('Calibre cannot publish outside its private stage.')
        if event in ('os.remove', 'os.rmdir', 'os.mkdir', 'os.chmod', 'os.utime', 'os.chdir'):
            if not inside(values[0], (stage,)):
                raise PermissionError('Calibre cannot mutate or enter an unselected path.')

    sys.addaudithook(audit)
    from calibre.constants import __version__
    if __version__ != '9.19.0':
        raise ValueError('This fixed wrapper has not been qualified for the selected calibre version.')
    from calibre.utils import safe_atexit
    _owned_temp_cleanup(safe_atexit, stage)
    if source.suffix == '.lit':
        # Upstream's freely generated level-1 LIT uses an all-zero content key.
        # A personalized bookplate or EUL license is never passed to its reader.
        with source.open('rb') as stream:
            raw = stream.read(64 * 1024**2 + 1)
        if b'/DRMStorage/Licenses/EUL' in raw or b'/DRMStorage/DRMBookplate' in raw:
            raise ValueError('Protected LIT inputs are not supported.')
        from calibre.ebooks.lit.reader import LitFile
        original = LitFile.read_drm

        def open_lit_only(self):
            if ('/DRMStorage/Licenses/EUL' in self.entries or '/DRMStorage/DRMBookplate' in self.entries):
                raise ValueError('Protected LIT inputs are not supported.')
            original(self)
            if self.drmlevel not in (0, 1) or self.drmlevel == 1 and self.bookkey != b'\0' * 8:
                raise ValueError('Only freely generated unprotected LIT content is admitted.')
        LitFile.read_drm = open_lit_only
    from calibre.ebooks.conversion.cli import main as convert
    result = convert(['ebook-convert', str(source), str(target),
                      '--txt-output-encoding=utf-8', '--txt-output-formatting=plain',
                      '--newline=unix', '--max-line-length=0',
                      '--disable-font-rescaling', '--keep-ligatures'])
    if result or not 0 < target.stat().st_size <= maximum:
        raise ValueError('Calibre did not produce a complete bounded reading copy.')


if __name__ == '__main__':
    main()
