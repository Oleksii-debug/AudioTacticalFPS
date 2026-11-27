"""Diagnostic only: observe the exact bounded runtime without accepting failures."""
from hashlib import sha256
import importlib.metadata
import json
from pathlib import Path
from time import monotonic

import quickjs
from carr_library.academic_integration import CitationRequest, formatting

rows = []
original_context, original_bridge = quickjs.Context, formatting._DOCUMENT_BRIDGE
references = [dict(id='book-doe', type='book', title='The Book of Science',
                   author=[{'family': 'Doe', 'given': 'Jane'}],
                   issued={'date-parts': [[2020]]}, publisher='Example Press', page='1-400')]
observations = []

class ObservedContext:
    def __init__(self):
        self.context = original_context()
        self.row = {'initial_memory': self.context.memory()}
        observations.append(self.row)

    def set_memory_limit(self, value):
        assert value == 256 * 1024 * 1024
        self.row['memory_limit'] = value
        return self.context.set_memory_limit(value)

    def set_time_limit(self, value):
        assert value == 30.0
        self.row['time_limit'] = value
        return self.context.set_time_limit(value)

    def set_max_stack_size(self, value):
        assert value == 4 * 1024 * 1024
        self.row['stack_limit'] = value
        return self.context.set_max_stack_size(value)

    def eval(self, code):
        self.row['code_bytes'] = len(code.encode('utf-8'))
        self.row['before_memory'] = self.context.memory()
        started = monotonic()
        try:
            return self.context.eval(code)
        except BaseException as error:
            self.row['error_type'] = type(error).__name__
            self.row['error'] = str(error)[:4096]
            raise
        finally:
            self.row['elapsed'] = monotonic() - started
            self.row['after_memory'] = self.context.memory()
            try:
                # Observation only after the business eval has finished/failed.
                # No engine call is retried and no runtime budget is increased.
                self.row['phase'] = self.context.eval('JSON.stringify({phase:globalThis.__carr_phase || null,error:globalThis.__carr_exception || null})')
            except BaseException as error:
                self.row['phase_read_error'] = str(error)[:1024]

phase_bridge = original_bridge
for old, new in (
    ("      const engine = new CSL.Engine(system, data.style_xml, data.locale, true);",
     "      globalThis.__carr_phase = 'engine-construction';\n      const engine = new CSL.Engine(system, data.style_xml, data.locale, true);\n      globalThis.__carr_phase = 'engine-created';"),
    ("      engine.updateItems(ordered);",
     "      globalThis.__carr_phase = 'update-items';\n      engine.updateItems(ordered);\n      globalThis.__carr_phase = 'items-updated';"),
    ("        const updates = engine.appendCitationCluster(citation);",
     "        globalThis.__carr_phase = 'append-citation';\n        const updates = engine.appendCitationCluster(citation);\n        globalThis.__carr_phase = 'citation-appended';"),
    ("      const bibliography = engine.makeBibliography();",
     "      globalThis.__carr_phase = 'bibliography';\n      const bibliography = engine.makeBibliography();\n      globalThis.__carr_phase = 'bibliography-created';"),
):
    assert phase_bridge.count(old) == 1
    phase_bridge = phase_bridge.replace(old, new)
phase_bridge = phase_bridge.replace("      'use strict';", "      'use strict';\n  try {", 1)
position = phase_bridge.rfind("})")
assert position > 0
phase_bridge = (phase_bridge[:position] +
    "  } catch (error) { globalThis.__carr_exception = {value:String(error), name:error && error.name, message:error && error.message, stack:error && String(error.stack).slice(0,4096)}; throw error; }\n" +
    phase_bridge[position:])

try:
    quickjs.Context = ObservedContext
    formatting._DOCUMENT_BRIDGE = phase_bridge
    for style in ('apa', 'chicago-author-date'):
        observations.clear()
        row = {'style': style, 'qualification': 'DIAGNOSTIC ONLY', 'runtime': importlib.metadata.version('quickjs-ng')}
        started = monotonic()
        try:
            result = formatting.render_citations(references, style=style,
                citations=[CitationRequest('book-doe', '12', locator_verified=True)])
            row.update(outcome='returned', bibliography=result.bibliography[0].text,
                       citation=result.citations[0].text)
        except BaseException as error:
            row.update(outcome='failed', exception=type(error).__name__, message=str(error))
        row['wall_seconds'] = monotonic() - started
        row['contexts'] = list(observations)
        rows.append(row)
        print(json.dumps(row, ensure_ascii=True, sort_keys=True), flush=True)
finally:
    quickjs.Context = original_context
    formatting._DOCUMENT_BRIDGE = original_bridge

destination = Path('ci-quickjs/runtime-diagnostics.json')
destination.write_text(json.dumps({
    'processor_sha256': sha256((formatting._ROOT/'vendor/citeproc.js').read_bytes()).hexdigest(),
    'representation': 'same verified style and locale JSON XML; diagnostic bridge phase markers only',
    'cases': rows,
}, ensure_ascii=True, indent=2), encoding='utf-8')
