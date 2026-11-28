"""Structured translation over the existing approved provider gateway and jobs.

Every paid request remains an ordinary TranslationService child. Scoped plans
add immutable selection, structural provenance and exact per-item cost review;
they never supply a second transport or retry an unresolved paid request.
"""
from dataclasses import asdict
from hashlib import sha256
import html
import json
from pathlib import Path
from uuid import uuid4

from .models import canonical, local_path
from .state import now
from .translation import TranslationService, _assemble_translation
from .translation_selection import blocks, revalidate_selection, validate_selection
from .translation_scope_state import (
    SCOPE_SCHEMA, ScopeApproval, TranslationScopeStore, child_title,
    estimate_micros, normalized_rate, review_digest, scope_budgets,
    validate_scope_links, validate_scope_plan)
from .translation_state import MAX_OUTPUT_CHARACTERS, split_text, text_hash
from ..providers import ProviderProfile, LOCAL_TRANSLATION_PROVIDER
from ..research.export_file import _destination, _filename, write_export
from ..research.topic_tree import TopicTree


class TranslationScopeService:
    def __init__(self, library, *, service=None):
        self.library = library
        self.service = service or TranslationService(library.root)
        self.root = local_path(library.root)
        if self.service.root != self.root:
            raise ValueError('Scoped translation must use the selected library and its existing provider service.')
        self.plans = TranslationScopeStore(self.root)

    def _authorize(self, selection):
        with self.library.read(selection['workspace']) as db:
            if selection['topic'] is not None:
                TopicTree(db, selection['workspace'])._visible(selection['topic'])

    def preview(self, profile, selection, source, target, budget_micros, *,
                glossary='', rate='', cancelled=lambda: False):
        local = profile.provider == LOCAL_TRANSLATION_PROVIDER
        if local and (type(budget_micros) is not int or budget_micros != 0 or rate not in (None, '') or glossary):
            raise ValueError('Local scoped translation requires zero remote reservation, no price and no glossary.')
        selection = validate_selection(selection)
        revalidate_selection(self.library, selection, cancelled=cancelled)
        budgets = scope_budgets(selection, budget_micros)
        rate = normalized_rate(rate)
        inventory, catalog = [], None
        for index, (unit, budget) in enumerate(zip(blocks(selection), budgets)):
            if cancelled():
                raise InterruptedError('Translation preview cancelled.')
            row = dict(index=index, item=unit['item'], section=unit['section'], block=unit['block'],
                       characters=len(unit['text']), calls=0, budget_micros=budget,
                       estimated_micros=0 if local or rate else None, source_sha256=text_hash(unit['text']))
            if unit['text'].strip():
                checked = self.service.preview(profile, unit['text'], source, target,
                                                rate_per_million=rate or None, glossary=glossary)
                if catalog is not None and checked['language_catalog_sha256'] != catalog:
                    raise ValueError('The supported language inventory changed during review.')
                catalog = checked['language_catalog_sha256']
                row.update(calls=checked['calls'], estimated_micros=0 if local else estimate_micros(len(unit['text']), rate))
                if row['estimated_micros'] is not None and row['estimated_micros'] > budget:
                    raise ValueError('An item price estimate exceeds its allocated reservation. Increase or review the total budget.')
            inventory.append(row)
        items = []
        for index, item in enumerate(selection['items']):
            rows = [row for row in inventory if row['item'] == index]
            items.append(dict(index=index, edition=item['edition'], revision=item['revision'],
                title=item['title'], chapter=item['chapter'], sections=len(item['sections']),
                blocks=len(rows), characters=sum(row['characters'] for row in rows),
                calls=sum(row['calls'] for row in rows), budget_micros=sum(row['budget_micros'] for row in rows),
                estimated_micros=0 if local else None if not rate else sum(row['estimated_micros'] for row in rows)))
        digest = review_digest(selection, profile, source, target, glossary, budget_micros, catalog, rate)
        return dict(review_sha256=digest, selection_sha256=text_hash(canonical(selection)),
            language_catalog_sha256=catalog, provider=profile.provider, provider_name=profile.name,
            endpoint=profile.endpoint, model=profile.model, glossary=glossary,
            source=source, target=target, budget_micros=budget_micros, currency='USD',
            rate_per_million=rate, items=items, blocks=inventory,
            characters=sum(row['characters'] for row in inventory), calls=sum(row['calls'] for row in inventory),
            estimated_micros=0 if local else None if not rate else sum(row['estimated_micros'] for row in inventory),
            estimate_basis=('selected local endpoint; zero remote reservation, runtime and quality not attested' if local
                            else 'owner-entered rate; local reservations are not independently verified provider bills'),
            preservation='source chapter/page order, original headings and exact source locators; no source-layout or translation-quality guarantee')

    def submit(self, profile, selection, source, target, budget_micros, *, approval,
               glossary='', rate='', cancelled=lambda: False):
        # Detach caller-owned structures before exact review or publication.
        selection = json.loads(canonical(validate_selection(selection)))
        preview = self.preview(profile, selection, source, target, budget_micros,
                               glossary=glossary, rate=rate, cancelled=cancelled)
        if (type(approval) is not ScopeApproval or not isinstance(approval.jobs, tuple)
                or approval.review_sha256 != preview['review_sha256']):
            raise PermissionError('Explicitly approve the current source manifest, item budgets and account.')
        stamp = now()
        plan = dict(schema=SCOPE_SCHEMA, id=approval.id, created=stamp, updated=stamp,
            status='preparing', library_root=str(self.root), ledger_identity=self.service._ledger_identity(),
            profile=json.loads(canonical(asdict(profile))), source_language=source, target_language=target,
            glossary=glossary, selection=selection, selection_sha256=preview['selection_sha256'],
            language_catalog_sha256=preview['language_catalog_sha256'], budget_micros=budget_micros,
            rate_per_million=preview['rate_per_million'], approved_review_sha256=approval.review_sha256,
            jobs=[json.loads(canonical(asdict(job))) if job is not None else None for job in approval.jobs])
        validate_scope_plan(plan)
        with self.plans.lock(plan['id']):
            self.plans.save(plan, new=True)
            # Durable plan precedes child creation. No provider call occurs here.
            # A crash leaves 'preparing'; missing children never trigger recreation.
            for unit, permit in zip(blocks(selection), approval.jobs):
                if cancelled():
                    raise InterruptedError('Scoped plan preparation stopped; no text was sent. Saved preparing plan: ' + plan['id'])
                if permit is not None:
                    self.service.submit(profile, unit['text'], source, target, approval=permit,
                                        title=child_title(plan, unit), glossary=glossary)
            validate_scope_links(plan, self.service.jobs.load)
            plan['status'] = 'queued'
            self.plans.save(plan)
        return plan['id']

    def _children(self, plan):
        return validate_scope_links(plan, self.service.jobs.load)

    def _summary(self, plan, jobs=None):
        jobs = self._children(plan) if jobs is None else jobs
        units = tuple(blocks(plan['selection']))
        finished = sum(job is None and not unit['text'].strip() or job is not None and job['status'] == 'complete'
                       for unit, job in zip(units, jobs))
        return dict(id=plan['id'], mode=plan['selection']['mode'], status=plan['status'],
                    documents=len(plan['selection']['items']), completed=finished, total=len(units),
                    source=plan['source_language'], target=plan['target_language'],
                    budget_micros=plan['budget_micros'],
                    billed_cost=('zero remote reservation; runtime operating costs not measured'
                                 if plan['profile']['provider'] == LOCAL_TRANSLATION_PROVIDER else
                                 'unknown; reservations only'))

    def resume(self, identity, *, cancelled=lambda: False, progress=lambda *args: None,
               expected_job_sha256=None):
        with self.plans.lock(identity):
            plan = self.plans.load(identity)
            if expected_job_sha256 is not None and (
                    not isinstance(expected_job_sha256, str)
                    or text_hash(canonical(plan)) != expected_job_sha256):
                raise PermissionError('The saved translation changed after review. Inspect it before an explicit run.')
            jobs = self._children(plan)
            if plan['status'] == 'preparing':
                raise PermissionError('This plan did not finish creating its approved children. No missing job may be recreated or billed automatically.')
            if plan['status'] == 'complete':
                for job in jobs:
                    if job is not None:
                        self.service._receipt_integrity(job)
                return self._summary(plan, jobs)
            if cancelled():
                plan['status'] = 'paused'
                self.plans.save(plan)
                return self._summary(plan, jobs)
            if plan['library_root'] != str(self.root) or plan['ledger_identity'] != self.service._ledger_identity():
                raise PermissionError('Moved or restored scoped plans cannot rebind saved remote permission.')
            self.service._current_profile(ProviderProfile.from_dict(plan['profile']))
            revalidate_selection(self.library, plan['selection'], cancelled=cancelled)
            plan['status'] = 'running'
            self.plans.save(plan)
            try:
                total_chunks = sum(len(split_text(unit['text'])) for unit in blocks(plan['selection']) if unit['text'].strip())
                for index, (unit, approval) in enumerate(zip(blocks(plan['selection']), plan['jobs'])):
                    if cancelled():
                        plan['status'] = 'paused'
                        self.plans.save(plan)
                        return self._summary(plan)
                    self._authorize(plan['selection'])
                    if approval is None:
                        continue
                    def accepted(done, total, phase):
                        current = self._children(plan)
                        size = sum(len(row['text']) for job in current if job is not None for row in job['receipts'])
                        if size > MAX_OUTPUT_CHARACTERS:
                            raise ValueError('Accepted scoped output exceeds eight million characters. It remains saved; no further request was sent.')
                        self._authorize(plan['selection'])
                        completed = sum(len(job['receipts']) for job in current if job is not None)
                        progress(completed, total_chunks, 'Accepted structured translation chunks')
                    result = self.service.resume(approval['job_id'], cancelled=cancelled, progress=accepted)
                    if result['status'] != 'complete':
                        plan['status'] = result['status']
                        self.plans.save(plan)
                        return self._summary(plan)
                plan['status'] = 'complete'
                jobs = self._children(plan)
                if sum(len(row['text']) for job in jobs if job is not None for row in job['receipts']) > MAX_OUTPUT_CHARACTERS:
                    raise ValueError('Scoped translation output exceeds the derived-export text budget.')
                self.plans.save(plan)
                return self._summary(plan, jobs)
            except BaseException:
                plan['status'] = 'attention'
                self.plans.save(plan)
                raise

    def list(self):
        return tuple(self._summary(self.plans.load(identity)) for identity in self.plans.identities())

    def charge_report(self, identity):
        plan = self.plans.load(identity)
        self._children(plan)
        return dict(plan=self._summary(plan), children=[
            self.service.charge_report(job['job_id']) for job in plan['jobs'] if job is not None],
            qualification='local reservations and receipt hashes; no independent paid-service verification')

    def export(self, identity, output, *, cancelled=lambda: False):
        target = local_path(output, must_exist=False)
        _filename(target.name)
        parent = local_path(target.parent)
        _destination(parent / ('.scope-translation-location-check-' + uuid4().hex), self.root)
        if cancelled():
            raise InterruptedError('Scoped translation export cancelled.')
        with self.plans.lock(identity):
            plan = self.plans.load(identity)
            if plan['status'] != 'complete':
                raise ValueError('Finish all selected items before publishing a complete derivative.')
            jobs = self._children(plan)
            units = tuple(blocks(plan['selection']))
            integrity = [self.service._receipt_integrity(job) if job is not None else [] for job in jobs]
            translated = [_assemble_translation(split_text(unit['text']), job['receipts'])
                          if job is not None else unit['text'] for unit, job in zip(units, jobs)]
            if sum(map(len, translated)) > MAX_OUTPUT_CHARACTERS:
                raise ValueError('Structured translation output exceeds its export budget.')
            documents, bilingual, plain, markup = [], [], [], [
                '<!doctype html><html><head><meta charset="utf-8">'
                '<meta http-equiv="Content-Security-Policy" content="default-src &#39;none&#39;">'
                '<title>Structured translation derivative</title></head><body>',
                '<h1>Machine translation derivative</h1>'
                '<p>Source headings and locators are preserved. This is not source quotation authority.</p>']
            receipts = []
            for item_index, item in enumerate(plan['selection']['items']):
                document = {key: value for key, value in item.items() if key != 'sections'}
                document['sections'] = []
                markup.append('<article><h2>' + html.escape(item['title']) + '</h2>')
                plain.append(item['title'])
                bilingual.append(item['title'])
                for section_index, section in enumerate(item['sections']):
                    positions = [i for i, unit in enumerate(units)
                                 if (unit['item'], unit['section']) == (item_index, section_index)]
                    values = [translated[i] for i in positions]
                    sources = [units[i]['text'] for i in positions]
                    joined = _assemble_translation(sources, [dict(text=value) for value in values])
                    location = section['locator']
                    label = section['heading'] + ' — ' + (
                        'PDF page ' if location['kind'] == 'physical_page' else 'Unpaginated segment ') + str(location['index'])
                    record = {key: value for key, value in section.items() if key != 'source_text'}
                    record.update(translated_text=joined, quotation_authority=False, blocks=[])
                    plain.extend((label, joined))
                    markup.append('<section><h3>' + html.escape(label) + '</h3>')
                    for i in positions:
                        unit, job = units[i], jobs[i]
                        accepted = [] if job is None else [
                            dict({key: value for key, value in row.items() if key != 'text'},
                                 decoded_receipt_integrity=level)
                            for row, level in zip(job['receipts'], integrity[i])]
                        record['blocks'].append(dict(source_start=unit['start'], source_end=unit['end'],
                            original_headings=unit['headings'], translated_text=translated[i],
                            job_id=None if job is None else job['id'], source_sha256=text_hash(unit['text']),
                            translated_sha256=text_hash(translated[i]), receipts=accepted))
                        receipts.append(dict(item=item_index, section=section_index, block=unit['block'],
                            job_id=None if job is None else job['id'], receipts=accepted))
                        bilingual.extend((label, 'ORIGINAL', unit['text'], 'TRANSLATION', translated[i]))
                        for heading in unit['headings']:
                            level = min(6, heading['level'] + 3)
                            markup.append('<h' + str(level) + '>Source heading: ' + html.escape(heading['title']) +
                                          '</h' + str(level) + '>')
                        markup.append('<div><h4>Original</h4><pre>' + html.escape(unit['text']) +
                                      '</pre><h4>Translation</h4><pre>' + html.escape(translated[i]) + '</pre></div>')
                    markup.append('</section>')
                    document['sections'].append(record)
                markup.append('</article>')
                documents.append(document)
            markup.append('</body></html>')
            structure = dict(schema='carr-structured-translation/1', plan_id=identity,
                workspace=plan['selection']['workspace'], mode=plan['selection']['mode'],
                source_language=plan['source_language'], target_language=plan['target_language'],
                source_is_immutable=True, quotation_authority=False, documents=documents)
            payloads = {
                'original-selected.json': canonical(plan['selection']) + '\n',
                'translation-structure.json': canonical(structure) + '\n',
                'translated.txt': '\n\n'.join(plain),
                'bilingual.txt': 'MACHINE TRANSLATION DERIVATIVE; NOT SOURCE QUOTATION AUTHORITY\n\n' + '\n\n'.join(bilingual),
                'bilingual.html': '\n'.join(markup)}
            if cancelled():
                raise InterruptedError('Scoped translation export cancelled.')
            target.mkdir(mode=0o700)
            files = {}
            for name, value in payloads.items():
                write_export(target / name, value, private_root=self.root, cancelled=cancelled, require_absent=True)
                files[name] = sha256(value.encode('utf-8')).hexdigest()
            report = dict(schema='carr-scoped-translation-export/1', plan_id=identity,
                selection_sha256=plan['selection_sha256'], approved_review_sha256=plan['approved_review_sha256'],
                source_is_immutable=True, quotation_authority=False, provider=plan['profile']['provider'],
                endpoint=plan['profile']['endpoint'], model=plan['profile']['model'], glossary=plan['glossary'],
                source_language=plan['source_language'], target_language=plan['target_language'],
                budget_micros=plan['budget_micros'], billed_cost='unknown; local reservations only',
                source_layout_preserved=False, structure_preserved='ordered selected sections, original headings and source block boundaries',
                translation_quality='not independently qualified', blocks=receipts, files=files)
            if plan['profile']['provider'] == LOCAL_TRANSLATION_PROVIDER:
                report.update(local_runtime_identity='not attested by the local API',
                    local_protocol='LibreTranslate selected loopback origin; credentials forbidden',
                    billed_cost='zero remote reservation; runtime operating costs not measured')
            # Report publication is the only complete-export marker.
            write_export(target / 'translation-provenance.json', canonical(report) + '\n',
                         private_root=self.root, cancelled=cancelled, require_absent=True)
            return dict(status='complete', path=str(target), files=files, documents=len(documents))
