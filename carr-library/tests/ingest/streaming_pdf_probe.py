"""Fresh-process measurements for one declared synthetic PDF container."""
import ctypes
import gc
import json
import os
from pathlib import Path
import platform
import shutil
import sys
from tempfile import TemporaryDirectory
from time import monotonic
import tracemalloc


def peak_rss():
    if os.name == 'nt':
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                ('PeakWorkingSetSize', ctypes.c_size_t), ('WorkingSetSize', ctypes.c_size_t),
                ('QuotaPeakPagedPoolUsage', ctypes.c_size_t), ('QuotaPagedPoolUsage', ctypes.c_size_t),
                ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t), ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t)]
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        psapi = ctypes.WinDLL('psapi', use_last_error=True)
        kernel.GetCurrentProcess.argtypes = []
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(counters.PeakWorkingSetSize)
    import resource
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == 'darwin' else value * 1024)


def _fixture():
    import importlib.util
    path = Path(__file__).with_name('streaming_pdf_fixture.py')
    spec = importlib.util.spec_from_file_location('_carr_streaming_pdf_fixture', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    fixture = _fixture()
    MIB, write_pdf = fixture.MIB, fixture.write_pdf
    # Include import costs in the baseline rather than attributing them to input size.
    import pypdf
    import pypdfium2
    from PIL import Image
    from carr_library.catalog.streaming_pdf import FileAsset, MAX_READ_BYTES, snapshot_original
    from carr_library.citations.pdf_review_attestation import render_fingerprint
    from carr_library.extract.page_ocr import extract_page
    from carr_library.ingest.retained_assets import RetainedAssets
    root, megabytes = Path(sys.argv[1]), int(sys.argv[2])
    if megabytes not in (72, 144, 2049):
        raise ValueError('Use one of the three declared synthetic fixture sizes.')
    available = shutil.disk_usage(root).free
    required = 2 * (megabytes + 1) * MIB + 256 * MIB
    if available < required:
        raise OSError(f'This declared PDF case needs {required} free bytes; observed {available}.')
    with TemporaryDirectory(prefix='large-pdf-proof-', dir=root) as temporary:
        work = Path(temporary)
        fixture_started = monotonic()
        source = write_pdf(work / 'source.pdf', megabytes * MIB)
        fixture_seconds = monotonic() - fixture_started
        originals = work / 'originals'
        originals.mkdir()
        gc.collect()
        before = peak_rss()
        tracemalloc.start()
        started = monotonic()
        asset = snapshot_original(work, source.name)
        assert isinstance(asset, FileAsset) and not hasattr(asset, 'data')
        retained = RetainedAssets(originals).retain_file(asset)
        copied = FileAsset.capture(retained, expected_manifest=asset.manifest())
        extracted = extract_page(copied, 1)
        assert extracted['status'] == 'candidate'
        assert extracted['provenance']['method'] == 'native'
        assert extracted['raw_text'].strip() == 'Large railway original'
        assert extracted['provenance']['asset_sha256'] == asset.digest
        renderer, fingerprint = render_fingerprint(copied, 1)
        assert renderer and len(fingerprint) == 64
        _, allocated = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        report = dict(schema=1, fixture='synthetic-unreferenced-stream-with-native-page',
            input_bytes=asset.size, source_sha256=asset.digest, elapsed_seconds=monotonic() - started,
            fixture_creation_seconds=fixture_seconds, free_bytes_before_fixture=available,
            declared_required_free_bytes=required, source_and_retained_bytes=2 * asset.size,
            peak_rss_bytes=peak_rss(), baseline_peak_rss_bytes=before,
            traced_peak_bytes=allocated, max_single_parser_read=MAX_READ_BYTES,
            profile=dict(os=platform.system(), release=platform.release(), machine=platform.machine(),
                         cpu_count=os.cpu_count(), python=platform.python_version(), isolated=bool(sys.flags.isolated)),
            native_text=True, render_fingerprint=True, retained_readback=True,
            real_corpus=False, multi_gigabyte_execution=asset.size > 2 * 1024 ** 3)
        import hashlib
        import importlib
        report['module_origins'] = {}
        for name in ('carr_library.catalog.streaming_pdf', 'carr_library.ingest.retained_assets',
                     'carr_library.extract.page_ocr', 'carr_library.citations.pdf_review_attestation'):
            module = importlib.import_module(name)
            path = Path(module.__file__).resolve(strict=True)
            report['module_origins'][name] = dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
