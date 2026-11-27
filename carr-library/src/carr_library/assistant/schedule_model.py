"""Portable schedule metadata inside the existing approved assistant job.

This module performs pure validation. A saved schedule is never a background
service or an authorization to run after restore; live arming is separate.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import re

from .models import canonical, text


SCHEMA = 'carr-assistant-schedule/1'
MAX_ARMED = 32
MAX_HORIZON = timedelta(days=366)
MAX_LATENESS_SECONDS = 86400
_STATES = frozenset({'pending', 'dispatching', 'complete', 'attention', 'disabled'})
_TIME = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)')


def utc(value):
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('Schedule timestamps need an explicit timezone.')
        return value.astimezone(timezone.utc)
    if type(value) is not str or _TIME.fullmatch(value) is None:
        raise ValueError('Use a complete UTC schedule timestamp with Z or +00:00.')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return parsed.astimezone(timezone.utc)


def timestamp(value):
    return utc(value).isoformat()


def fingerprint(value):
    return sha256(canonical(value).encode('utf-8')).hexdigest()


def validate_schedule(value, record):
    required = {'schema', 'created', 'due', 'expires', 'root_identity', 'plan_sha256',
                'state', 'started', 'finished', 'message'}
    if (type(value) is not dict or set(value) != required or value['schema'] != SCHEMA
            or type(value['state']) is not str or value['state'] not in _STATES):
        raise ValueError('Invalid saved assistant schedule.')
    for key in ('created', 'due', 'expires'):
        if type(value[key]) is not str:
            raise ValueError('Saved schedule timestamps must be exact UTC strings.')
    created, due, expires = (utc(value[key]) for key in ('created', 'due', 'expires'))
    if (not created <= due < expires or due - created > MAX_HORIZON
            or not timedelta(seconds=1) <= expires - due <= timedelta(seconds=MAX_LATENESS_SECONDS)):
        raise ValueError('The saved schedule exceeds its approved time window.')
    identity = value['root_identity']
    if (type(identity) is not list or len(identity) != 2
            or any(type(item) is not int or not 0 <= item < 2**128 for item in identity)
            or identity[1] == 0):
        raise ValueError('Invalid local schedule directory identity.')
    if (type(value['plan_sha256']) is not str
            or re.fullmatch('[0-9a-f]{64}', value['plan_sha256']) is None
            or value['plan_sha256'] != record['plan_sha256']):
        raise ValueError('A schedule cannot change its approved plan.')
    text(value['message'], 'schedule status', maximum=1000, empty=True)
    for key in ('started', 'finished'):
        if value[key] is not None and type(value[key]) is not str:
            raise ValueError('Invalid saved schedule event time.')
    started = None if value['started'] is None else utc(value['started'])
    finished = None if value['finished'] is None else utc(value['finished'])
    if started is not None and not due <= started < expires:
        raise ValueError('A scheduled attempt began outside its reviewed time window.')
    if finished is not None and finished < (started if started is not None else created):
        raise ValueError('The schedule event order is inconsistent.')
    state = value['state']
    if state == 'pending' and (started is not None or finished is not None
            or record['status'] != 'queued'
            or any(step['status'] != 'pending' or step['intent'] or step['result'] is not None
                   for step in record['steps'])):
        raise ValueError('Only a never-started queued plan may have a pending schedule.')
    if state == 'dispatching' and (started is None or finished is not None or record['status'] != 'running'):
        raise ValueError('A dispatching schedule needs its durable running intent.')
    if state == 'complete' and (started is None or finished is None or record['status'] != 'complete'
            or any(step['status'] != 'complete' for step in record['steps'])):
        raise ValueError('Schedule completion requires the verified complete assistant job.')
    return value


@dataclass(frozen=True)
class ScheduleView:
    id: str
    due: str
    expires: str
    plan_sha256: str
    schedule_sha256: str
    state: str
    job_status: str
    message: str
    local_binding_valid: bool
