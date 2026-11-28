"""Typed scoped translation plans backed by the existing translation job store.

Plans retain exact source structure and child approvals. Backup validation
checks every declared child against that immutable plan; restore never changes
the saved library, provider or ledger binding.
"""
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, ROUND_UP
import json
import re
from uuid import uuid4

from .models import canonical, text
from .state import now, read_bytes, state_write_guard
from .translation_selection import blocks, validate_selection
from .translation_state import (
    ADVANCED_SCHEMA, SCHEMA, LOCAL_SCHEMA, TranslationStore, allocations, approval_from_dict,
    split_text, text_hash, validate_translation_job, _hash, _stamp)
from ..providers import ProviderProfile, LOCAL_TRANSLATION_PROVIDER

SCOPE_SCHEMA = 'carr-translation-scope/1'
MAX_SCOPES = 100
MAX_SCOPE_BYTES = 20 * 1024 * 1024
SCOPE_NAME = re.compile(r'scope-([0-9a-f]{32})\.json')


def scope_budgets(selection, budget_micros):
    units = tuple(blocks(validate_selection(selection)))
    if type(budget_micros) is not int or not 0 <= budget_micros <= 10**12:
        raise ValueError('An explicit bounded integer USD reservation is required.')
    weights = [len(unit['text']) if unit['text'].strip() else 0 for unit in units]
    total = sum(weights)
    result = [budget_micros * weight // total if weight else 0 for weight in weights]
    remainder = budget_micros - sum(result)
    for index in sorted(range(len(weights)), key=lambda n: (-(budget_micros * weights[n] % total), n)):
        if not remainder:
            break
        if weights[index]:
            result[index] += 1
            remainder -= 1
    if remainder or sum(result) != budget_micros:
        raise ValueError('Translation reservation allocation failed.')
    return tuple(result)


def normalized_rate(rate):
    if rate in (None, ''):
        return ''
    if not isinstance(rate, (str, int, Decimal)) or isinstance(rate, bool):
        raise ValueError('Enter a finite owner-provided USD price per million characters.')
    try:
        result = Decimal(rate)
        if not result.is_finite() or not 0 <= result <= Decimal('1000000'):
            raise ValueError
        value = format(result, 'f')
        if len(value) > 64:
            raise ValueError
        return value
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('Enter a finite owner-provided USD price per million characters.') from None


def estimate_micros(characters, rate):
    rate = normalized_rate(rate)
    return None if rate == '' else int((Decimal(rate) * characters).to_integral_value(rounding=ROUND_UP))


def review_digest(selection, profile, source, target, glossary, budget_micros,
                  language_catalog_sha256, rate=''):
    validate_selection(selection)
    scope_budgets(selection, budget_micros)
    _hash(language_catalog_sha256)
    return text_hash(canonical(dict(selection=selection, profile_fingerprint=profile.fingerprint,
        source=source, target=target, glossary=glossary, budget_micros=budget_micros,
        language_catalog_sha256=language_catalog_sha256, rate=normalized_rate(rate))))


@dataclass(frozen=True)
class ScopeApproval:
    id: str
    review_sha256: str
    jobs: tuple


def scope_approval(profile, selection, source, target, budget_micros, *,
                   language_catalog_sha256, glossary='', rate=''):
    """Call only after the owner reviews these exact per-item transfers."""
    from .translation import translation_approval
    budgets = scope_budgets(selection, budget_micros)
    digest = review_digest(selection, profile, source, target, glossary, budget_micros,
                           language_catalog_sha256, rate)
    jobs = tuple(translation_approval(profile, unit['text'], source, target, budget, glossary=glossary)
                 if unit['text'].strip() else None
                 for unit, budget in zip(blocks(selection), budgets))
    return ScopeApproval(uuid4().hex, digest, jobs)


def child_title(plan, unit):
    item = plan['selection']['items'][unit['item']]
    section = item['sections'][unit['section']]
    return (item['title'][:180] + ' / ' + section['heading'][:220] +
            ' / block ' + str(unit['block'] + 1))[:512]


def child_template(plan, unit, approval):
    profile = ProviderProfile.from_dict(plan['profile'])
    schema = (LOCAL_SCHEMA if profile.provider == LOCAL_TRANSLATION_PROVIDER else
              ADVANCED_SCHEMA if profile.provider == 'google-translation-v3' else SCHEMA)
    value = dict(schema=schema, id=approval['job_id'], library_root=plan['library_root'],
        ledger_identity=list(plan['ledger_identity']), created=plan['created'], updated=plan['updated'],
        status='queued', profile=plan['profile'], title=child_title(plan, unit), source_text=unit['text'],
        source_sha256=text_hash(unit['text']), source_language=plan['source_language'],
        target_language=plan['target_language'], language_catalog_sha256=plan['language_catalog_sha256'],
        approval=approval, allocations=allocations(split_text(unit['text']), approval['budget_micros']),
        receipts=[], pending=None)
    if schema == ADVANCED_SCHEMA:
        value['glossary'] = plan['glossary']
    return value


def validate_scope_plan(value, *, identity=None):
    keys = {'schema', 'id', 'created', 'updated', 'status', 'library_root', 'ledger_identity',
            'profile', 'source_language', 'target_language', 'glossary', 'selection',
            'selection_sha256', 'language_catalog_sha256', 'budget_micros',
            'rate_per_million', 'approved_review_sha256', 'jobs'}
    if (type(value) is not dict or set(value) != keys or value['schema'] != SCOPE_SCHEMA
            or not isinstance(value['id'], str) or not re.fullmatch('[0-9a-f]{32}', value['id'])
            or (identity is not None and value['id'] != identity)):
        raise ValueError('Invalid scoped translation checkpoint.')
    _stamp(value['created'])
    _stamp(value['updated'])
    if value['status'] not in ('preparing', 'queued', 'running', 'paused', 'attention', 'complete'):
        raise ValueError('Invalid scoped translation status.')
    selection = validate_selection(value['selection'])
    if value['selection_sha256'] != text_hash(canonical(selection)):
        raise ValueError('The immutable translation selection changed.')
    profile = ProviderProfile.from_dict(value['profile'])
    if profile.provider == LOCAL_TRANSLATION_PROVIDER and (
            value['budget_micros'] != 0 or value['rate_per_million'] != '' or value['glossary'] != ''):
        raise ValueError('Local scoped translation requires zero remote reservation, no price and no glossary.')
    digest = review_digest(selection, profile, value['source_language'], value['target_language'],
        value['glossary'], value['budget_micros'], value['language_catalog_sha256'], value['rate_per_million'])
    if value['approved_review_sha256'] != digest:
        raise ValueError('The reviewed transfer manifest changed.')
    if value['rate_per_million'] != normalized_rate(value['rate_per_million']):
        raise ValueError('Invalid scoped translation price preview.')
    units = tuple(blocks(selection))
    budgets = scope_budgets(selection, value['budget_micros'])
    jobs = value['jobs']
    if type(jobs) is not list or len(jobs) != len(units):
        raise ValueError('Scoped translation child inventory changed.')
    identifiers = {value['id']}
    for unit, budget, job in zip(units, budgets, jobs):
        if not unit['text'].strip():
            if job is not None:
                raise ValueError('Whitespace cannot create a paid translation child.')
            continue
        approval = approval_from_dict(job)
        if approval.job_id in identifiers or approval.budget_micros != budget:
            raise ValueError('Scoped translation child identities or reservations changed.')
        identifiers.add(approval.job_id)
        validate_translation_job(child_template(value, unit, job))
    if len((canonical(value) + '\n').encode('utf-8')) > MAX_SCOPE_BYTES:
        raise ValueError('Scoped translation checkpoint exceeds its byte budget.')
    return value


def validate_scope_links(plan, load_job):
    """Every referenced child keeps exact request authority and immutable input."""
    validate_scope_plan(plan)
    result = []
    for unit, approval in zip(blocks(plan['selection']), plan['jobs']):
        if approval is None:
            result.append(None)
            continue
        try:
            job = load_job(approval['job_id'])
        except FileNotFoundError:
            if plan['status'] == 'preparing':
                result.append(None)
                continue
            raise ValueError('A scoped translation child is missing. It must not be recreated or rebilled.') from None
        validate_translation_job(job, identity=approval['job_id'])
        expected = child_template(plan, unit, approval)
        for key in expected:
            if key not in ('created', 'updated', 'status', 'receipts', 'pending') and job.get(key) != expected[key]:
                raise ValueError('A scoped translation child differs from its reviewed source or approval.')
        if plan['status'] == 'complete' and job['status'] != 'complete':
            raise ValueError('A completed scoped translation omitted an unfinished child.')
        result.append(job)
    return tuple(result)


class TranslationScopeStore(TranslationStore):
    def path(self, identity):
        plain = super().path(identity)
        return plain.with_name('scope-' + plain.name)

    def load(self, identity):
        from .validation import decode_state_json
        value = decode_state_json(read_bytes(self.path(identity), maximum=MAX_SCOPE_BYTES))
        return validate_scope_plan(value, identity=identity)

    def save(self, value, *, new=False):
        value['updated'] = now()
        validate_scope_plan(value)
        self._publish(self.path(value['id']), value, new=new)

    def identities(self):
        from .translation_state import state_paths
        with state_write_guard(self.directory):
            return tuple(SCOPE_NAME.fullmatch(path.name)[1] for path in state_paths(self.root)
                         if SCOPE_NAME.fullmatch(path.name))
