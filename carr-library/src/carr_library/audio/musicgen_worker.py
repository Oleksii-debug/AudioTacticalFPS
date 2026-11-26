"""Isolated optional local music workers. No downloads, shell or remote code.

This file is executed with Python -I by the reviewed parent adapter. A worker
never publishes to the user's destination; it writes only private stage files.
"""
import ctypes
from hashlib import sha256
import importlib.metadata
import json
import math
import os
from pathlib import Path
import random
import re
import socket
import stat
import sys
import wave


def deny_network():
    """Install a Python guard, including saved and low-level socket constructors.

    Audit hooks also cover original constructors retained before this call. This
    does not intercept a native library's direct system calls and is not an OS
    sandbox. MusicGen separately requires a verified isolated network namespace.
    """
    local_family = getattr(socket, 'AF_UNIX', None)
    def audit(event, args):
        if event == 'socket.__new__':
            if local_family is None or len(args) < 2 or args[1] != local_family:
                raise PermissionError('This music worker denies Python Internet sockets.')
        elif event in {'socket.connect', 'socket.bind', 'socket.sendto', 'socket.sendmsg'}:
            if not args or local_family is None or getattr(args[0], 'family', None) != local_family:
                raise PermissionError('This music worker denies Python Internet transport.')
        elif event in {'socket.getaddrinfo', 'socket.gethostbyname', 'socket.gethostbyaddr',
                       'socket.getnameinfo', 'urllib.Request', 'subprocess.Popen',
                       'os.system', 'os.posix_spawn', 'os.exec', 'os.spawn'}:
            raise PermissionError('This music worker denies network lookup and child execution.')
    sys.addaudithook(audit)
    original = socket.socket
    class LocalSocket(original):
        def connect(self, address):
            if self.family != local_family:
                raise PermissionError('This music worker is offline-only.')
            return super().connect(address)
        def connect_ex(self, address):
            if self.family != local_family:
                raise PermissionError('This music worker is offline-only.')
            return super().connect_ex(address)
        def sendto(self, *args, **kwargs):
            if self.family != local_family:
                raise PermissionError('This music worker is offline-only.')
            return super().sendto(*args, **kwargs)
    def denied(*args, **kwargs):
        raise PermissionError('This music worker is offline-only.')
    socket.socket = LocalSocket
    # The audit hook guards _socket and previously saved native constructors.
    # Replacing _socket.socket with a Python subclass would recurse through
    # socket.socket.__init__, which delegates to that exact C constructor.
    socket.create_connection = denied
    socket.getaddrinfo = denied
    os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                      HF_HUB_DISABLE_TELEMETRY='1', HF_HUB_DISABLE_IMPLICIT_TOKEN='1',
                      DO_NOT_TRACK='1', TOKENIZERS_PARALLELISM='false')


def require_network_isolation():
    """Fail closed unless kernel state establishes an isolated Linux namespace.

    No environment flag or manifest assertion is accepted as isolation proof.
    Windows needs a separately reviewed native isolation launcher; a Python
    monkeypatch cannot make that guarantee for Torch/CUDA or other native code.
    """
    if not sys.platform.startswith('linux'):
        raise RuntimeError('MusicGen is unavailable: verified native network isolation is not implemented on this platform.')
    try:
        current = os.readlink('/proc/self/ns/net')
        initial = os.readlink('/proc/1/ns/net')
        interfaces = {name for _, name in socket.if_nameindex()}
    except OSError as exc:
        raise RuntimeError('MusicGen is unavailable: cannot verify the local worker network namespace.') from exc
    if current == initial or interfaces != {'lo'}:
        raise RuntimeError('MusicGen is unavailable: run the approved worker in a separate Linux network namespace with only loopback; Python socket guards do not isolate native libraries.')
    return {'mechanism': 'separate Linux network namespace', 'external_interfaces': 0,
            'python_network_guard': True, 'native_runtime_trusted': True}


def plain_local(path, *, directory=False):
    """Check every component before resolution, including Windows redirection."""
    raw = os.fspath(path)
    if (not isinstance(raw, str) or not raw or raw.startswith(('//', '\\\\'))
            or '://' in raw or any(ord(c) < 32 for c in raw)):
        raise ValueError('Choose an ordinary local model path.')
    value = Path(raw).absolute()
    if any(':' in part for part in value.parts[1:]):
        raise ValueError('Model paths cannot contain alternate data streams.')
    for component in (value, *value.parents):
        details = component.lstat()
        if (stat.S_ISLNK(details.st_mode)
                or getattr(details, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)):
            raise ValueError('Model paths cannot contain links or reparse points.')
    if os.name == 'nt':
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32.dll', use_last_error=True, winmode=0x800)
        kernel.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
        kernel.GetDriveTypeW.restype = wintypes.UINT
        if kernel.GetDriveTypeW(value.anchor) == 4:
            raise ValueError('Mapped network drives are not local model installations.')
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(value.lstat().st_mode):
        raise ValueError('Expected a plain local model directory.' if directory else 'Expected a regular local model file.')
    return value.resolve(strict=True)


def _relative_asset(value):
    if (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.\-/]{1,240}', value)
            or value.startswith('/') or any(p in ('', '.', '..') for p in value.split('/'))):
        raise ValueError('Model references must name files inside the approved manifest.')
    return value


def _json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate model configuration field.')
            result[key] = value
        return result
    def invalid(value):
        raise ValueError('Nonfinite model configuration value.')
    try:
        return json.loads(data, object_pairs_hook=unique, parse_constant=invalid)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError('Invalid bounded model configuration JSON.') from exc


def validate_model_assets(root, manifest, *, cancelled=None, require_inference_assets=False):
    """Verify inventory and all loadable JSON references before optional imports.

    This validates data boundaries, not model quality or runtime trust. The
    manifest's external approved digest is checked by the calling adapter.
    """
    root = plain_local(root, directory=True)
    entries = manifest.get('files') if isinstance(manifest, dict) else None
    if not isinstance(entries, list) or not 1 <= len(entries) <= 2000:
        raise ValueError('Invalid MusicGen model file inventory.')
    expected, documents, total, json_bytes = set(), {}, 0, 0
    def check():
        if cancelled is not None:
            active = cancelled.is_set() if hasattr(cancelled, 'is_set') else cancelled()
            if active:
                raise InterruptedError('Model verification cancelled.')
    for entry in entries:
        check()
        if not isinstance(entry, dict) or set(entry) != {'path', 'sha256', 'size_bytes'}:
            raise ValueError('Invalid model file record.')
        relative = _relative_asset(entry['path'])
        if Path(relative).name in {'adapter_config.json', 'quantize_config.json', 'quant_config.json'}:
            raise ValueError('Unreviewed model adapters and quantization loaders are unsupported.')
        if (relative in expected or not (relative.lower().endswith(('.json', '.safetensors', '.model', '.txt', '.md'))
                or Path(relative).name in ('LICENSE', '.gitattributes'))):
            raise ValueError('Only passive local model files and safetensors weights are allowed.')
        size, wanted = entry['size_bytes'], entry['sha256']
        if type(size) is not int or not 0 < size <= 32 * 1024**3:
            raise ValueError('Invalid model file size.')
        total += size
        if total > 64 * 1024**3:
            raise ValueError('Model installation exceeds the 64 GiB verification budget.')
        if not isinstance(wanted, str) or not re.fullmatch(r'[0-9a-f]{64}', wanted):
            raise ValueError('Invalid model checksum.')
        path = plain_local(root / relative)
        before = path.stat()
        if not path.is_relative_to(root) or before.st_size != size:
            raise ValueError('Installed MusicGen model failed its file verification.')
        h, chunks = sha256(), []
        if relative.lower().endswith('.json') and size > 16 * 1024**2:
            raise ValueError('Model JSON exceeds the configuration byte budget.')
        if relative.lower().endswith('.json'):
            json_bytes += size
            if json_bytes > 64 * 1024**2:
                raise ValueError('Model JSON exceeds the total configuration byte budget.')
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                check()
                h.update(block)
                if relative.lower().endswith('.json'):
                    chunks.append(block)
        after = path.stat()
        if (h.hexdigest() != wanted or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            raise ValueError('Installed MusicGen model failed its file verification.')
        if relative.lower().endswith('.json'):
            documents[relative] = _json(b''.join(chunks))
        expected.add(relative)
    actual = set()
    for count, path in enumerate(root.rglob('*'), 1):
        check()
        if count > 16000:
            raise ValueError('Model directory entry budget exceeded.')
        details = path.lstat()
        if (stat.S_ISLNK(details.st_mode)
                or getattr(details, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)):
            raise ValueError('Model directories must not contain links.')
        if stat.S_ISREG(details.st_mode):
            actual.add(path.relative_to(root).as_posix())
        elif not stat.S_ISDIR(details.st_mode):
            raise ValueError('Model directories contain a non-regular asset.')
    if actual != expected or 'config.json' not in expected or not any(n.endswith('.safetensors') for n in expected):
        raise ValueError('MusicGen model inventory is incomplete or contains unexpected files.')
    def reference(value):
        if value is None:
            return
        if isinstance(value, list):
            for item in value:
                reference(item)
            return
        if _relative_asset(value) not in expected:
            raise ValueError('Model configuration references an asset outside the approved inventory.')
    def inspect(value, depth=0):
        if depth > 48:
            raise ValueError('Model configuration nesting budget exceeded.')
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {'auto_map', 'custom_pipelines', 'trust_remote_code', 'code_revision',
                           'pretrained', 'pretrained_model_name_or_path', 'compression_model_checkpoint',
                           'checkpoint', 'repo_id', 'tokenizer_name', 'config_name',
                           'custom_generate', 'quantization_config', 'peft_config'}:
                    raise ValueError('Remote code, remote codec and implicit model references are not supported.')
                classes = {'processor_class': {'MusicgenProcessor'},
                           'tokenizer_class': {'T5Tokenizer', 'T5TokenizerFast'},
                           'feature_extractor_type': {'EncodecFeatureExtractor'}}
                if key in classes and item is not None and (not isinstance(item, str) or item not in classes[key]):
                    raise ValueError('Unreviewed MusicGen processor or tokenizer class.')
                # These are data strings, never model-loader filenames.
                if key in {'_name_or_path', 'name_or_path', 'vocab', 'merges', 'added_tokens',
                           'additional_special_tokens', 'special_tokens', 'added_tokens_decoder'}:
                    continue
                if key.endswith(('_file', '_files', '_path', '_paths', '_dir', '_dirs', '_folder', '_folders')):
                    reference(item)
                else:
                    inspect(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                inspect(item, depth + 1)
    for name, document in documents.items():
        if name.endswith('.safetensors.index.json'):
            if (not isinstance(document, dict) or set(document) != {'metadata', 'weight_map'}
                    or not isinstance(document['metadata'], dict)
                    or not isinstance(document['weight_map'], dict) or not document['weight_map']):
                raise ValueError('Invalid safetensors shard index.')
            for shard in document['weight_map'].values():
                if not isinstance(shard, str):
                    raise ValueError('Shard references must name approved safetensors files.')
                reference(shard)
                if not shard.endswith('.safetensors'):
                    raise ValueError('Shard references must use approved safetensors files.')
        elif Path(name).name not in {'added_tokens.json', 'special_tokens_map.json'}:
            inspect(document)
    if require_inference_assets:
        cfg = documents['config.json']
        if (not isinstance(cfg, dict) or cfg.get('model_type') != 'musicgen'
                or any(not isinstance(cfg.get(key), dict) or cfg[key].get('model_type') != kind
                       for key, kind in (('audio_encoder', 'encodec'), ('text_encoder', 't5'), ('decoder', 'musicgen_decoder')))):
            raise ValueError('MusicGen is unavailable: install a complete reviewed text-to-music configuration; melody/style models are unsupported here.')
        if not {'preprocessor_config.json', 'tokenizer_config.json', 'special_tokens_map.json'} <= expected:
            raise ValueError('MusicGen is unavailable: approved processor and tokenizer assets are incomplete.')
        if not {'spiece.model', 'tokenizer.json'} & expected:
            raise ValueError('MusicGen is unavailable: an approved local tokenizer vocabulary is required.')
        if 'model.safetensors' not in expected and 'model.safetensors.index.json' not in expected:
            raise ValueError('MusicGen is unavailable: install complete safetensors weights and their approved shard index.')
    return root


def digest(path):
    h = sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def _musicgen(request):
    manifest = request['manifest']
    root = validate_model_assets(request['model_dir'], manifest, require_inference_assets=True)
    isolation = require_network_isolation()
    for distribution, key in (('transformers', 'transformers_version'), ('torch', 'torch_version')):
        if importlib.metadata.version(distribution) != manifest[key]:
            raise ValueError(f'The installed {distribution} version does not match the approved model environment.')
    import torch
    from transformers import AutoProcessor, MusicgenForConditionalGeneration
    spec = request['spec']
    device = request['device']
    if device.startswith('cuda') and not torch.cuda.is_available():
        raise RuntimeError('The selected CUDA device is unavailable; no implicit hardware fallback.')
    torch.manual_seed(spec['seed'])
    torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
    processor = AutoProcessor.from_pretrained(str(root), local_files_only=True, trust_remote_code=False, token=False)
    model = MusicgenForConditionalGeneration.from_pretrained(
        str(root), local_files_only=True, trust_remote_code=False, use_safetensors=True, token=False)
    model.to(device)
    model.eval()
    text = spec['prompt'] or f"Instrumental {spec['mood']} music, {spec['tempo_bpm']} BPM, {spec['key']} key, no vocals"
    inputs = processor(text=[text], padding=True, return_tensors='pt').to(device)
    rate = int(model.config.audio_encoder.sampling_rate)
    frame_rate = float(model.config.audio_encoder.frame_rate)
    # MusicGen delays codebooks. Account for the tail tokens instead of
    # padding an incomplete model output with invented silent samples.
    delay = model.config.decoder.num_codebooks // model.config.decoder.audio_channels - 1
    tokens = math.ceil(spec['duration_seconds'] * frame_rate) + delay
    if not 1 <= tokens <= 1500:
        raise ValueError('MusicGen segment exceeds its reviewed generation context.')
    with torch.inference_mode():
        waveform = model.generate(**inputs, do_sample=True, guidance_scale=3.0,
                                  max_new_tokens=tokens)[0].detach().float().cpu()
    frames = round(spec['duration_seconds'] * rate)
    if waveform.ndim != 2 or waveform.shape[0] not in (1, 2) or waveform.shape[1] < frames:
        raise RuntimeError('MusicGen returned an incomplete or unsupported waveform.')
    waveform = waveform[:, :frames]
    if not torch.isfinite(waveform).all():
        raise RuntimeError('MusicGen returned non-finite audio samples.')
    peak = float(waveform.abs().max())
    attenuation = min(1., .95 / peak) if peak else 1.
    waveform *= attenuation
    pcm = (waveform.transpose(0, 1) * 32767).round().to(torch.int16).numpy()
    if sys.byteorder != 'little':
        pcm = pcm.byteswap()
    output = Path(request['output'])
    with output.open('xb') as stream:
        with wave.open(stream, 'wb') as audio:
            audio.setparams((int(waveform.shape[0]), 2, rate, 0, 'NONE', 'not compressed'))
            for start in range(0, frames, 4096):
                audio.writeframesraw(pcm[start:start+4096].tobytes())
        stream.flush()
        os.fsync(stream.fileno())
    return dict(engine='musicgen', status='complete', frames=frames, sample_rate=rate,
                channels=int(waveform.shape[0]), gain_to_avoid_clipping=attenuation,
                device=device, seed=spec['seed'], model_id=manifest['model_id'],
                network_isolation=isolation, continuation=False)


def _fluidsynth(request):
    sf2 = Path(request['soundfont'])
    if digest(sf2) != request['soundfont_sha256']:
        raise ValueError('SoundFont identity changed before rendering.')
    lib = ctypes.CDLL(request['library'])
    def function(name, result, *args):
        item = getattr(lib, name)
        item.restype, item.argtypes = result, list(args)
        return item
    ptr, i, c, d = ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_double
    new_settings = function('new_fluid_settings', ptr)
    del_settings = function('delete_fluid_settings', None, ptr)
    setting_num = function('fluid_settings_setnum', i, ptr, c, d)
    setting_int = function('fluid_settings_setint', i, ptr, c, i)
    new_synth = function('new_fluid_synth', ptr, ptr)
    del_synth = function('delete_fluid_synth', None, ptr)
    sfload = function('fluid_synth_sfload', i, ptr, c, i)
    program = function('fluid_synth_program_select', i, ptr, i, i, i, i)
    note_on = function('fluid_synth_noteon', i, ptr, i, i, i)
    all_off = function('fluid_synth_all_notes_off', i, ptr, i)
    render = function('fluid_synth_write_s16', i, ptr, i, ptr, i, i, ptr, i, i)
    version = function('fluid_version_str', c)
    rate, spec = request['sample_rate'], request['spec']
    settings, synth = new_settings(), None
    if not settings:
        raise RuntimeError('Could not initialize FluidSynth settings.')
    try:
        for key, value in ((b'synth.sample-rate', rate), (b'synth.gain', .35)):
            if setting_num(settings, key, value) != 0:
                raise RuntimeError('FluidSynth rejected a numeric setting.')
        for key, value in ((b'synth.reverb.active', 0), (b'synth.chorus.active', 0),
                           (b'synth.cpu-cores', 1), (b'synth.threadsafe-api', 0)):
            if setting_int(settings, key, value) != 0:
                raise RuntimeError('FluidSynth rejected a runtime setting.')
        synth = new_synth(settings)
        if not synth:
            raise RuntimeError('Could not initialize FluidSynth.')
        identifier = sfload(synth, os.fsencode(sf2), 1)
        if identifier < 0:
            raise RuntimeError('The selected SoundFont could not be loaded.')
        for channel, preset in ((0, 0), (1, 48), (2, 32)):
            if program(synth, channel, identifier, 0, preset) != 0:
                raise RuntimeError('SoundFont does not provide the required GM instruments.')
        key = {'C': 0, 'C#': 1, 'Db': 1, 'D': 2, 'D#': 3, 'Eb': 3, 'E': 4,
               'F': 5, 'F#': 6, 'Gb': 6, 'G': 7, 'G#': 8, 'Ab': 8, 'A': 9,
               'A#': 10, 'Bb': 10, 'B': 11}[spec['key']]
        root = 48 + key
        minor = spec['mood'] in ('suspense', 'dramatic', 'sad')
        scale = (0, 2, 3, 5, 7, 8, 10) if minor else (0, 2, 4, 5, 7, 9, 11)
        randomizer = random.Random(spec['seed'])
        beat = 60 / spec['tempo_bpm']
        total = round(spec['duration_seconds'] * rate)
        position, beat_index = 0, 0
        buffer = (ctypes.c_int16 * (2048 * 2))()
        with Path(request['output']).open('xb') as stream:
            while position < total:
                for channel in range(3):
                    all_off(synth, channel)
                chord = (0, 5, 3 if minor else 4, 7)[(beat_index // 4) % 4]
                notes = ((0, root+12+scale[randomizer.randrange(len(scale))], 62),
                         (1, root+chord, 32), (1, root+chord+(3 if minor else 4), 28),
                         (1, root+chord+7, 25), (2, root+chord-12, 45))
                for channel, pitch, velocity in notes:
                    if note_on(synth, channel, pitch, velocity) != 0:
                        raise RuntimeError('FluidSynth rejected a composed note.')
                end = min(total, round((beat_index + 1) * beat * rate))
                while position < end:
                    count = min(2048, end - position)
                    if render(synth, count, buffer, 0, 2, buffer, 1, 2) != 0:
                        raise RuntimeError('FluidSynth could not render the next audio block.')
                    data = bytes(buffer)[:count*4]
                    if sys.byteorder != 'little':
                        import array
                        samples = array.array('h')
                        samples.frombytes(data)
                        samples.byteswap()
                        data = samples.tobytes()
                    stream.write(data)
                    position += count
                beat_index += 1
            stream.flush()
            os.fsync(stream.fileno())
        return dict(engine='fluidsynth', status='complete', frames=total, sample_rate=rate,
                    channels=2, library_version=version().decode('ascii'), seed=spec['seed'])
    finally:
        if synth:
            del_synth(synth)
        del_settings(settings)


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        raise ValueError('One private worker request is required.')
    path = plain_local(arguments[0])
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('Invalid private worker request.')
    with path.open('rb') as stream:
        content = stream.read(2 * 1024 * 1024 + 1)
    if len(content) > 2 * 1024 * 1024:
        raise ValueError('Invalid private worker request.')
    request = _json(content)
    if (not isinstance(request, dict) or type(request.get('version')) is not int
            or request.get('version') != 1 or request.get('engine') not in ('musicgen', 'fluidsynth')):
        raise ValueError('Unsupported worker request.')
    for name in ('output', 'result'):
        destination = Path(request[name])
        if (not destination.is_absolute() or plain_local(destination.parent, directory=True) != path.parent
                or os.path.lexists(destination)):
            raise ValueError('Worker outputs must be fresh files in the private stage.')
    deny_network()
    report = _musicgen(request) if request['engine'] == 'musicgen' else _fluidsynth(request)
    with Path(request['result']).open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f'Local music worker: {type(exc).__name__}: {exc}', file=sys.stderr)
        raise SystemExit(1)
