"""Read-only failure evidence for one explicitly selected CI virtual input.

This script never records samples or alters consent, services, drivers, device
defaults or input formats. It reuses the existing diagnostic moniker/WaveInID/
opaque endpoint-ID crosswalk. Observed native failures are evidence, never a
substitute for the unchanged real capture acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import traceback

BRANCH = 'refs/heads/carr/ci-endpoint-evaluation-20261127'
REPOSITORY = 'Oleksii-debug/AudioTacticalFPS'
FRIENDLY_NAME = 'CABLE Output (VB-Audio Virtual Cable)'
MAX_RECEIPT_BYTES = 128 * 1024
MAX_NATIVE_BYTES = 256 * 1024


def _evaluation_guard():
    if (sys.platform != 'win32' or os.environ.get('GITHUB_ACTIONS') != 'true'
            or os.environ.get('GITHUB_EVENT_NAME') != 'push'
            or os.environ.get('GITHUB_REPOSITORY') != REPOSITORY
            or os.environ.get('GITHUB_REF') != BRANCH
            or re.fullmatch(r'[0-9a-f]{40}', os.environ.get('GITHUB_SHA', '')) is None):
        raise RuntimeError('This read-only diagnostic requires the exact disposable endpoint evaluation.')


def _moniker(selected, friendly):
    if (type(selected) is not str or not selected.startswith('@device_cm_')
            or not 12 <= len(selected) <= 2048 or '\x00' in selected
            or friendly != FRIENDLY_NAME):
        raise ValueError('The exact selected reviewed input is required.')


def _original_selection(value):
    if (type(value) is not dict or value.get('status') != 'FAILED'
            or value.get('actual_directshow_capture') is not False
            or type(value.get('selected_device')) is not dict):
        raise ValueError('An actual failed capture receipt with its explicit selected input is required.')
    selected = value['selected_device']
    if (set(selected) != {'id', 'name'} or type(selected['id']) is not str
            or re.fullmatch(r'[0-9a-f]{64}', selected['id']) is None
            or selected['name'] != FRIENDLY_NAME):
        raise ValueError('The failed capture did not identify the exact reviewed virtual input.')
    return dict(selected)


def _select(devices, original):
    if type(devices) not in (tuple, list) or not 1 <= len(devices) <= 64:
        raise ValueError('The fresh input inventory is absent or exceeds its diagnostic bound.')
    matches = [item for item in devices if item.id == original['id']]
    named = [item for item in devices if item.name == FRIENDLY_NAME]
    if len(matches) != 1 or len(named) != 1 or matches[0] is not named[0]:
        raise ValueError('The original selected input is unavailable, changed or ambiguous; no fallback is allowed.')
    device = matches[0]
    _moniker(device.alternative, device.name)
    if hashlib.sha256(device.alternative.encode('utf-8')).hexdigest() != original['id']:
        raise ValueError('The selected opaque moniker no longer has the original device identity.')
    return device


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('A native diagnostic JSON field is repeated.')
        result[key] = value
    return result


def _nonfinite(value):
    raise ValueError('A native diagnostic JSON number is not finite.')


def _native_result(raw, returncode, selected):
    if type(raw) is not bytes or not 1 <= len(raw) <= MAX_NATIVE_BYTES:
        raise ValueError('Native diagnostic output is absent or exceeds its byte bound.')
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                       parse_constant=_nonfinite)
    if (type(value) is not dict or value.get('status') not in ('DIAGNOSED', 'FAILED')
            or type(returncode) is not int
            or (returncode == 0) != (value.get('status') == 'DIAGNOSED')
            or value.get('scope') != 'selected moniker only; no recording started'
            or value.get('selected_moniker') != selected.alternative
            or value.get('selected_friendly_name') != selected.name
            or value.get('physical_microphone') != 'NOT_RUN'
            or value.get('policy_changes') is not False
            or value.get('fallback') is not False):
        raise ValueError('The native diagnostic did not retain its exact selection or read-only result contract.')
    wasapi = value.get('wasapi')
    if wasapi is not None or value['status'] == 'DIAGNOSED':
        if (type(wasapi) is not dict
                or any(wasapi.get(name) is not False for name in
                       ('capture_started', 'buffers_requested', 'property_changes', 'device_fallback'))
                or wasapi.get('shared_mode') is not True):
            raise ValueError('The native WASAPI observations do not preserve their no-capture contract.')
    return value


def _native_command(device):
    return [sys.executable, '-I', '-B', '-X', 'utf8', str(Path(__file__).resolve(strict=True)),
            '--native-open', device.alternative, '--friendly-name', device.name]


def _new_report():
    return {'schema': 'carr-ci-capture-access-diagnostic/1', 'status': 'FAILED',
            'application_acceptance': 'NOT_ASSESSED', 'capture_started': False,
            'sample_buffers_requested': False, 'device_fallback': False,
            'policy_changes': False, 'physical_microphone': 'NOT_RUN',
            'human_listening': 'NOT_RUN', 'nvda': 'NOT_RUN'}


def run(args, report):
    _evaluation_guard()
    report.update(source_commit=os.environ['GITHUB_SHA'], python=sys.version,
                  executable=sys.executable)
    # This fixed read-only inventory remains useful even when the selected
    # capture receipt is absent or refuses rebinding; it never authorizes open.
    report['access_settings'] = _access_settings()
    from carr_library.audio.cleanup import retained_temporary_directory
    from carr_library.audio.models import AudioBudget
    from carr_library.audio.process import run_process
    from carr_library.audio.recording import RecordingService
    from carr_library.durable_state import digest_file, read_json_snapshot

    original, token = read_json_snapshot(args.capture_report, max_bytes=MAX_RECEIPT_BYTES)
    selected = _original_selection(original)
    report['failed_capture'] = {'sha256': token, 'selected_device': selected,
                                'status': original['status']}
    report['script_sha256'] = digest_file(Path(__file__))
    report['ffmpeg_sha256'] = digest_file(Path(args.ffmpeg))
    with retained_temporary_directory(prefix='carr-input-open-diagnostic-', dir=None) as temporary:
        library = Path(temporary) / 'library'
        library.mkdir()
        service = RecordingService(library, ffmpeg=args.ffmpeg)
        device = _select(service.devices(), selected)
        report['selected_device'] = {'id': device.id, 'name': device.name,
                                     'alternative': device.alternative}
        if digest_file(Path(args.ffmpeg)) != report['ffmpeg_sha256']:
            raise ValueError('The explicit FFmpeg changed during fresh input discovery.')
        current, current_token = read_json_snapshot(args.capture_report, max_bytes=MAX_RECEIPT_BYTES)
        if current_token != token or _original_selection(current) != selected:
            raise ValueError('The original failed capture changed before native diagnosis.')
        budget = AudioBudget(timeout=18, max_output_bytes=1024 * 1024,
            max_log_bytes=MAX_NATIVE_BYTES, max_alloc_bytes=64 * 1024 * 1024, threads=1)
        result = run_process(_native_command(device), budget=budget,
                             owned_tree=True, allow_failure=True)
        report['native_child'] = {'exit_code': result.returncode,
                                 'stderr': result.stderr.decode('utf-8', errors='replace')}
        report['native_input_open'] = _native_result(result.stdout, result.returncode, device)
        report['status'] = 'DIAGNOSED' if result.returncode == 0 else 'FAILED'
    return report


def _access_settings():
    _evaluation_guard()
    import ctypes
    from ctypes import wintypes
    import json
    import os
    from pathlib import Path
    import re
    import sys
    import winreg

    report = {
        'status': 'DIAGNOSED', 'registry_writes': False, 'consent_changes': False,
        'device_access_attempt': False, 'physical_microphone': 'NOT_RUN',
        'windows': tuple(sys.getwindowsversion()), 'settings': [],
    }
    def read_value(label, hive, key, value):
        result = {'label': label, 'key': key, 'value_name': value}
        try:
            with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as handle:
                data, kind = winreg.QueryValueEx(handle, value)
            result['registry_type'] = kind
            if type(data) is int and 0 <= data <= 0xffffffff:
                result['value'] = data
            elif isinstance(data, str) and data in ('Allow', 'Deny', 'Prompt', 'UserInControl'):
                result['value'] = data
            else:
                result['value'] = 'OTHER_REDACTED'
        except FileNotFoundError:
            result['state'] = 'NOT_CONFIGURED_AT_THIS_KEY'
        except OSError as exc:
            result['state'] = 'READ_FAILED'
            result['winerror'] = exc.winerror
        report['settings'].append(result)

    consent = r'SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone'
    for label, hive in (('current-user', winreg.HKEY_CURRENT_USER), ('machine', winreg.HKEY_LOCAL_MACHINE)):
        read_value(label, hive, consent, 'Value')
        read_value(label + '-desktop', hive, consent + r'\NonPackaged', 'Value')
    policy = r'SOFTWARE\Policies\Microsoft\Windows\AppPrivacy'
    for name in ('LetAppsAccessMicrophone', 'LetDesktopAppsAccessMicrophone',
                 'LetAppsAccessMicrophone_ForceAllowUnsigned', 'LetAppsAccessMicrophone_ForceDenyUnsigned'):
        read_value('machine-policy', winreg.HKEY_LOCAL_MACHINE, policy, name)
    terminal = r'SOFTWARE\Policies\Microsoft\Windows NT\Terminal Services'
    for name in ('fDisableAudioCapture', 'fDisableAudio'):
        read_value('remote-session-policy', winreg.HKEY_LOCAL_MACHINE, terminal, name)

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    query = kernel.ProcessIdToSessionId
    query.argtypes = (wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    query.restype = wintypes.BOOL
    session = wintypes.DWORD()
    if query(os.getpid(), ctypes.byref(session)):
        report['process_session_id'] = session.value
    else:
        report['session_query_error'] = ctypes.get_last_error()
    name = os.environ.get('SESSIONNAME', '')
    report['session_name'] = name if re.fullmatch(r'Console|Services|RDP-Tcp#\d{1,8}', name) else 'UNSET_OR_OTHER'
    report['sources'] = [
        'https://learn.microsoft.com/en-us/windows/win32/api/mmdeviceapi/nf-mmdeviceapi-activateaudiointerfaceasync',
        'https://learn.microsoft.com/en-us/windows/client-management/mdm/policy-csp-privacy',
    ]
    return report


def _native_open(selected, friendly):
    _evaluation_guard()
    _moniker(selected, friendly)
    import ctypes as C
    from ctypes import wintypes as W
    import json
    import os
    import sys
    from comtypes import GUID
    from comtypes.automation import VARIANT

    P, HR = C.c_void_p, C.c_long
    ole = C.OleDLL('ole32')
    ole.CoInitializeEx.argtypes = [P, W.DWORD]
    ole.CoInitializeEx.restype = HR
    ole.CoUninitialize.argtypes = []
    ole.CoUninitialize.restype = None
    ole.CoCreateInstance.argtypes = [C.POINTER(GUID), P, W.DWORD, C.POINTER(GUID), C.POINTER(P)]
    ole.CoCreateInstance.restype = HR
    ole.CreateBindCtx.argtypes = [W.DWORD, C.POINTER(P)]
    ole.CreateBindCtx.restype = HR
    ole.CoTaskMemFree.argtypes = [P]
    ole.CoTaskMemFree.restype = None
    oleaut = C.OleDLL('oleaut32')
    oleaut.VariantClear.argtypes = [C.POINTER(VARIANT)]
    oleaut.VariantClear.restype = HR

    def method(pointer, offset, args=(), result=HR):
        table = C.cast(pointer, C.POINTER(C.POINTER(P))).contents
        return C.WINFUNCTYPE(result, P, *args)(table[offset])

    def release(pointer):
        if pointer:
            method(pointer, 2, result=W.ULONG)(pointer)

    def ok(code, label):
        if code < 0:
            raise OSError(f'{label}: HRESULT 0x{code & 0xffffffff:08x}')
        return code

    def property_value(bag, name):
        value = VARIANT()
        try:
            ok(method(bag, 3, (W.LPCWSTR, C.POINTER(VARIANT), P))(bag, name, C.byref(value), None), 'IPropertyBag.Read ' + name)
            return value.value
        finally:
            oleaut.VariantClear(C.byref(value))

    class WaveFormat(C.Structure):
        _pack_ = 1
        _fields_ = [('tag', W.WORD), ('channels', W.WORD), ('rate', W.DWORD),
                    ('average', W.DWORD), ('alignment', W.WORD),
                    ('bits', W.WORD), ('extra', W.WORD)]

    def inspect_wasapi(winmm, wave_id, report):
        # Primary ABI: Microsoft win32metadata mmdeviceapi.h, Audioclient.h,
        # mmddk.h, and the documented legacy waveform endpoint-ID bridge.
        # Open the exact same capture endpoint without Start/GetBuffer.
        result = {'scope': 'exact selected native input only',
                  'capture_started': False, 'buffers_requested': False,
                  'shared_mode': True, 'property_changes': False,
                  'device_fallback': False, 'calls': []}
        report['wasapi'] = result
        winmm.waveInMessage.argtypes = [P, W.UINT, C.c_size_t, C.c_size_t]
        winmm.waveInMessage.restype = W.UINT

        def mm_id(label):
            size = C.c_size_t(0)
            code = int(winmm.waveInMessage(P(wave_id), 0x0800 + 18,
                                          C.addressof(size), 0))
            result['calls'].append({'operation': label + '.size',
                                    'mmresult': code})
            if code:
                return None
            if not 2 <= size.value <= 16384 or size.value % 2:
                raise RuntimeError('Exact native endpoint ID exceeds its bound.')
            buffer = C.create_unicode_buffer(size.value // 2)
            code = int(winmm.waveInMessage(P(wave_id), 0x0800 + 17,
                                          C.addressof(buffer), size.value))
            result['calls'].append({'operation': label + '.value',
                                    'mmresult': code})
            if code:
                return None
            value = C.string_at(C.addressof(buffer), size.value).decode('utf-16le')
            if not value.endswith('\x00') or not value[:-1] or '\x00' in value[:-1]:
                raise RuntimeError('Exact native endpoint ID is not bounded terminated text.')
            return value[:-1]

        def observed(label, code):
            result['calls'].append({'operation': label,
                                    'hresult': f'0x{code & 0xffffffff:08x}'})
            return code >= 0

        mm_enum, device, endpoint, client, mix = (P() for _ in range(5))
        try:
            endpoint_id = mm_id('waveIn.endpoint.before')
            if endpoint_id is None:
                return
            result['endpoint_id'] = endpoint_id
            # GetDevice consumes this opaque ID; never parse its contents
            # and never choose an endpoint by name, role, or default.
            clsid = GUID('{BCDE0395-E52F-467C-8E3D-C4579291692E}')
            iid = GUID('{A95664D2-9614-4F35-A746-DE8DB63617E6}')
            create = C.WinDLL('ole32', winmode=0x00000800).CoCreateInstance
            create.argtypes = [C.POINTER(GUID), P, W.DWORD,
                               C.POINTER(GUID), C.POINTER(P)]
            create.restype = HR
            code = create(C.byref(clsid), None, 1, C.byref(iid), C.byref(mm_enum))
            if not observed('CoCreateInstance.IMMDeviceEnumerator', code):
                return
            if not mm_enum:
                raise RuntimeError('The native endpoint enumerator is absent.')
            code = method(mm_enum, 5, (W.LPCWSTR, C.POINTER(P)))(
                mm_enum, endpoint_id, C.byref(device))
            if not observed('IMMDeviceEnumerator.GetDevice', code):
                return
            if not device:
                raise RuntimeError('The selected native endpoint is absent.')
            state = W.DWORD()
            code = method(device, 6, (C.POINTER(W.DWORD),))(device, C.byref(state))
            if not observed('IMMDevice.GetState.before', code):
                return
            result['state_before'] = int(state.value)
            endpoint_iid = GUID('{1BE09788-6894-4089-8586-9A2A6C265AC5}')
            code = method(device, 0, (C.POINTER(GUID), C.POINTER(P)))(
                device, C.byref(endpoint_iid), C.byref(endpoint))
            if not observed('QueryInterface.IMMEndpoint', code):
                return
            if not endpoint:
                raise RuntimeError('The native endpoint data-flow interface is absent.')
            flow = C.c_int(-1)
            code = method(endpoint, 3, (C.POINTER(C.c_int),))(endpoint, C.byref(flow))
            if not observed('IMMEndpoint.GetDataFlow', code):
                return
            result['data_flow'] = int(flow.value)
            if flow.value != 1:
                raise RuntimeError('The exact selected endpoint is not a capture endpoint.')
            if state.value != 1:
                result['not_initialized'] = 'Selected capture endpoint is not active.'
                return
            client_iid = GUID('{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}')
            code = method(device, 3, (C.POINTER(GUID), W.DWORD, P, C.POINTER(P)))(
                device, C.byref(client_iid), 1, None, C.byref(client))
            if not observed('IMMDevice.Activate.IAudioClient', code):
                return
            if not client:
                raise RuntimeError('The exact selected audio client is absent.')
            default_period, minimum_period = C.c_longlong(), C.c_longlong()
            code = method(client, 9, (C.POINTER(C.c_longlong), C.POINTER(C.c_longlong)))(
                client, C.byref(default_period), C.byref(minimum_period))
            if observed('IAudioClient.GetDevicePeriod', code):
                result['device_period_100ns'] = {'default': default_period.value,
                                                'minimum': minimum_period.value}
            code = method(client, 8, (C.POINTER(P),))(client, C.byref(mix))
            if not observed('IAudioClient.GetMixFormat', code):
                return
            if not mix or C.sizeof(WaveFormat) != 18:
                raise RuntimeError('Native mix format or WAVEFORMATEX ABI is unavailable.')
            wave = C.cast(mix, C.POINTER(WaveFormat)).contents
            if (not 1 <= wave.channels <= 64 or not 1 <= wave.rate <= 768000
                    or not 1 <= wave.bits <= 64 or not 0 <= wave.extra <= 65518
                    or not 1 <= wave.alignment <= 65535
                    or not 1 <= wave.average <= 0xffffffff):
                raise RuntimeError('Native mix format exceeds its diagnostic bound.')
            result['mix_format'] = {'tag': int(wave.tag), 'channels': int(wave.channels),
                'rate': int(wave.rate), 'bits': int(wave.bits),
                'block_align': int(wave.alignment), 'average_bytes_per_second': int(wave.average),
                'extra_bytes': int(wave.extra)}
            # No forced PCM format, exclusive mode, session settings,
            # event callback, capture service, or sample buffer request.
            if mm_id('waveIn.endpoint.pre_initialize') != endpoint_id:
                raise RuntimeError('Selected native endpoint changed before initialization.')
            code = method(client, 3,
                (C.c_int, W.DWORD, C.c_longlong, C.c_longlong, P, C.POINTER(GUID)))(
                client, 0, 0, 0, 0, mix, None)
            result['stream_initialized'] = observed('IAudioClient.Initialize.shared', code)
            code = method(device, 6, (C.POINTER(W.DWORD),))(device, C.byref(state))
            if observed('IMMDevice.GetState.after', code):
                result['state_after'] = int(state.value)
            result['endpoint_unchanged'] = mm_id('waveIn.endpoint.after') == endpoint_id
            if not result['endpoint_unchanged']:
                raise RuntimeError('Selected native endpoint changed during initialization.')
        finally:
            # Releasing an initialized-but-never-started client closes
            # the stream. GetMixFormat storage is independently owned.
            for pointer in (client, endpoint, device, mm_enum):
                release(pointer)
            if mix:
                ole.CoTaskMemFree(mix)

    report = {'status': 'DIAGNOSED', 'scope': 'selected moniker only; no recording started',
              'selected_moniker': selected, 'selected_friendly_name': friendly,
              'physical_microphone': 'NOT_RUN', 'policy_changes': False, 'fallback': False}
    enumerator = monikers = context = chosen = capture_filter = pins = P()
    initialized = False
    try:
        # comtypes initialized this thread already; an additional balanced STA entry.
        ok(ole.CoInitializeEx(None, 2), 'CoInitializeEx')
        initialized = True
        enumerator, monikers, context, chosen, capture_filter, pins = (P() for _ in range(6))
        clsid = GUID('{62BE5D10-60EB-11D0-BD3B-00A0C911CE86}')
        enum_iid = GUID('{29840822-5B84-11D0-BD3B-00A0C911CE86}')
        category = GUID('{33D9A762-90C8-11D0-BD43-00A0C911CE86}')
        ok(ole.CoCreateInstance(C.byref(clsid), None, 1, C.byref(enum_iid), C.byref(enumerator)), 'Create system enumerator')
        ok(ole.CreateBindCtx(0, C.byref(context)), 'CreateBindCtx')
        code = method(enumerator, 3, (C.POINTER(GUID), C.POINTER(P), W.DWORD))(enumerator, C.byref(category), C.byref(monikers), 0)
        if code != 0 or not monikers:
            raise RuntimeError('Audio moniker inventory is absent.')
        matches = 0
        for _ in range(64):
            moniker, name_pointer = P(), P()
            fetched = W.ULONG()
            code = method(monikers, 3, (W.ULONG, C.POINTER(P), C.POINTER(W.ULONG)))(monikers, 1, C.byref(moniker), C.byref(fetched))
            if code == 1:
                break
            ok(code, 'IEnumMoniker.Next')
            if fetched.value != 1 or not moniker:
                raise RuntimeError('Invalid moniker enumeration.')
            try:
                ok(method(moniker, 20, (P, P, C.POINTER(P)))(moniker, context, None, C.byref(name_pointer)), 'GetDisplayName')
                display = C.wstring_at(name_pointer)
                if display.replace(':', '_') == selected:
                    matches += 1
                    if chosen:
                        raise RuntimeError('Selected moniker is ambiguous.')
                    chosen, moniker = moniker, P()
            finally:
                if name_pointer:
                    ole.CoTaskMemFree(name_pointer)
                release(moniker)
        else:
            raise RuntimeError('Audio moniker inventory exceeds its bound.')
        if matches != 1:
            raise RuntimeError('Selected moniker changed before native inspection.')
        bag = P()
        bag_iid = GUID('{55272A00-42CB-11CE-8135-00AA004BB851}')
        ok(method(chosen, 9, (P, P, C.POINTER(GUID), C.POINTER(P)))(chosen, context, None, C.byref(bag_iid), C.byref(bag)), 'BindToStorage')
        try:
            if property_value(bag, 'FriendlyName') != friendly:
                raise RuntimeError('Selected moniker display identity changed.')
            wave_id = property_value(bag, 'WaveInID')
            if type(wave_id) is not int or not 0 <= wave_id < 64:
                raise RuntimeError('Moniker does not supply a bounded native WaveInID.')
            report['wave_in_id'] = wave_id
        finally:
            release(bag)
        winmm = C.WinDLL('winmm')
        winmm.waveInGetNumDevs.argtypes = []
        winmm.waveInGetNumDevs.restype = W.UINT
        if not wave_id < winmm.waveInGetNumDevs() <= 64:
            raise RuntimeError('Native input inventory changed.')
        winmm.waveInOpen.argtypes = [C.POINTER(P), W.UINT, C.POINTER(WaveFormat), C.c_size_t, C.c_size_t, W.DWORD]
        winmm.waveInOpen.restype = W.UINT
        winmm.waveInClose.argtypes = [P]
        winmm.waveInClose.restype = W.UINT
        winmm.waveInGetErrorTextW.argtypes = [W.UINT, W.LPWSTR, W.UINT]
        winmm.waveInGetErrorTextW.restype = W.UINT
        def explanation(code):
            text = C.create_unicode_buffer(512)
            result = winmm.waveInGetErrorTextW(code, text, len(text))
            return text.value if result == 0 else 'No driver error text.'
        report['wave_in_trials'] = []
        for rate in (44100, 48000):
            for channels in (1, 2):
                wave = WaveFormat(1, channels, rate, rate * channels * 2, channels * 2, 16, 0)
                for flags in (1, 9, 0):
                    handle = P()
                    result = int(winmm.waveInOpen(C.byref(handle), wave_id, C.byref(wave), 0, 0, flags))
                    row = {'rate': rate, 'channels': channels, 'bits': 16,
                           'operation': 'open-without-buffers' if flags == 0 else 'query',
                           'direct': bool(flags & 8), 'result': result, 'error': explanation(result)}
                    report['wave_in_trials'].append(row)
                    if handle:
                        closed = int(winmm.waveInClose(handle))
                        row['close_result'] = closed
                        if closed:
                            raise RuntimeError('Exact native input handle did not close.')
        inspect_wasapi(winmm, wave_id, report)

    except BaseException as exc:
        report['status'] = 'FAILED'
        report['error'] = type(exc).__name__ + ': ' + str(exc)
        raise
    finally:
        for pointer in (pins, capture_filter, chosen, context, monikers, enumerator):
            release(pointer)
        if initialized:
            ole.CoUninitialize()
        print(json.dumps(report, ensure_ascii=False), flush=True)


def _save_report(path, report):
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n',
                    encoding='utf-8')


def _wait_for_pending(owners, interrupted=None):
    """Keep this standalone process alive until each existing owner is quiet.

    This uses the same synchronous proof as the installer. It creates no
    worker, lease or replacement owner. Record interruptions and deliver them
    only after every original causal resource has actually settled.
    """
    for owner in owners:
        while True:
            try:
                if owner.quiescent_event.is_set():
                    break
                owner.close()
                owner.quiescent_event.wait(.25)
            except (KeyboardInterrupt, SystemExit) as signal:
                if interrupted is None:
                    interrupted = signal
    return interrupted


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ffmpeg')
    parser.add_argument('--capture-report', type=Path)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--native-open', help=argparse.SUPPRESS)
    parser.add_argument('--friendly-name', help=argparse.SUPPRESS)
    args = parser.parse_args(arguments)
    _evaluation_guard()
    if args.native_open is not None:
        if (args.friendly_name is None or args.ffmpeg is not None
                or args.capture_report is not None or args.report is not None):
            parser.error('The private native child accepts only its exact selected moniker and name.')
        _native_open(args.native_open, args.friendly_name)
        return 0
    if (not args.ffmpeg or args.capture_report is None or args.report is None
            or args.friendly_name is not None):
        parser.error('Supply the current explicit FFmpeg, failed capture receipt and diagnostic report.')
    report = _new_report()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    try:
        run(args, report)
    except BaseException as error:
        report['status'] = 'FAILED'
        report['error'] = traceback.format_exc()
        from carr_library.audio.cleanup import _pending_owners
        owners = _pending_owners(error)
        if owners:
            report['cleanup_pending'] = True
            report['cleanup_owner_count'] = len(owners)
            # Preserve the failed observations before a potentially prolonged
            # cleanup wait. Failure to write must not release the native owner.
            interrupted = None
            try:
                _save_report(args.report, report)
            except (KeyboardInterrupt, SystemExit) as signal:
                interrupted = signal
            except Exception as write_error:
                report['early_report_error'] = type(write_error).__name__ + ': ' + str(write_error)[:512]
            interrupted = _wait_for_pending(owners, interrupted)
            report['cleanup_pending'] = False
            report['cleanup_quiescent'] = True
            if interrupted is not None:
                raise interrupted from error
        raise
    finally:
        _save_report(args.report, report)
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if report['status'] == 'DIAGNOSED' else 1


if __name__ == '__main__':
    raise SystemExit(main())
