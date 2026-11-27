"""A bounded full-source reader, separate from search and citation approval.

One desk is pinned to the active sealed revision of one authorized edition.
Every page action opens a fresh snapshot, authenticates the retained original,
and consults the existing independent text attestors. Reading an unreviewed
page is allowed and clearly labelled; it never creates quotation authority.
"""
from dataclasses import dataclass
import re

from ..catalog.membership import Catalog
from ..catalog.read_views import get_edition
from ..citations.anchors import Identity, Locator, Metadata, digest
from ..research.topic_tree import TopicTree
from .reading_state import ReadingLocation


MAX_PAGES = 10000
MAX_PAGE_CHARACTERS = 2_000_000


def _id(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 1024:
        raise ValueError(f'Invalid {label}.')
    return value


def _locator(row):
    if type(row['ordinal']) is not int or row['ordinal'] < 1:
        raise ValueError('The source has an invalid page order.')
    if type(row['verified']) is not int or row['verified'] not in (0, 1):
        raise ValueError('The source has an invalid printed-page state.')
    if row['physical'] is not None:
        return Locator('physical_page', row['physical'], row['printed'], bool(row['verified']))
    return Locator('logical_segment', row['ordinal'], row['printed'], bool(row['verified']))


def _unapproved_locator(locator):
    # A stored printed-page flag is not independent approval. The ordinary
    # reader must not describe an unapproved printed label as an actual page.
    return Locator(locator.kind, locator.index)


@dataclass(frozen=True)
class PageDescriptor:
    ordinal: int
    locator: Locator
    text_sha256: str

    @property
    def index(self):
        return self.locator.index

    @property
    def kind(self):
        return self.locator.kind

    @property
    def description(self):
        return _unapproved_locator(self.locator).display()


@dataclass(frozen=True)
class ReadingPage:
    location: ReadingLocation
    locator: Locator
    raw_text: str
    metadata: Metadata
    verified: bool
    ordinal: int
    total: int

    @property
    def text(self):
        return self.raw_text

    @property
    def title(self):
        return self.metadata.title or 'Unknown title'

    @property
    def author(self):
        return self.metadata.author or 'Unknown author'

    @property
    def status(self):
        if self.verified:
            return 'Source text verified. Reading does not create or change a quotation.'
        return 'Extracted text is unverified and requires independent review before quotation.'

    @property
    def description(self):
        unit = 'Page' if self.locator.kind == 'physical_page' else 'Segment'
        return (f'{self.title}. {self.author}. {self.locator.display()}. '
                f'{unit} {self.ordinal} of {self.total} in reading order. {self.status}')


class ReadingDesk:
    """Read one exact edition without manufacturing a search selection.

    ``page`` accepts a 1-based reading-order ordinal, not a physical PDF index.
    The descriptor's locator preserves the actual source index, including gaps.
    ``validate`` resolves a saved location and fails on any revision, original,
    source-text or authorization drift; it never retargets a newer edition.
    """

    def __init__(self, library, workspace, edition, topic=None, revision=None, *,
                 cancelled=lambda: False, timeout=60.0):
        self.library = library
        self.workspace = _id(workspace, 'library identity')
        self.edition = _id(edition, 'edition identity')
        self.topic = _id(topic, 'topic identity') if topic is not None else None
        if revision is not None:
            _id(revision, 'revision identity')
        if not callable(cancelled):
            raise ValueError('A cancellation callback is required.')
        self.cancelled = cancelled
        self.timeout = timeout
        with library._search_read(workspace, timeout, cancelled) as (db, budget):
            identity, metadata = self._header(db)
            if revision is not None and identity.revision != revision:
                raise ValueError('The saved reading revision is no longer active. Open the book again.')
            from ..ingest.source_profiles import LargeTextProfile, profile_from_config
            extraction = db.execute('SELECT config FROM revision WHERE id=?',
                                    (identity.revision,)).fetchone()
            profile = profile_from_config(extraction[0]) if extraction is not None else None
            maximum_pages = profile.maximum_records if isinstance(profile, LargeTextProfile) else MAX_PAGES
            rows = db.execute(
                'SELECT ordinal,physical,printed,verified,digest FROM page '
                'WHERE revision=? ORDER BY ordinal LIMIT ?',
                (identity.revision, maximum_pages + 1)).fetchall()
            if not rows or len(rows) > maximum_pages:
                raise ValueError('The source has no readable pages or exceeds the reader page limit.')
            descriptors = []
            previous = None
            for expected, row in enumerate(rows, 1):
                budget.check()
                locator = _locator(row)
                if row['ordinal'] != expected or (previous is not None and (
                        locator.kind != previous.kind or locator.index <= previous.index)):
                    raise ValueError('The source has an invalid page or segment sequence.')
                if not isinstance(row['digest'], str) or not re.fullmatch('[0-9a-f]{64}', row['digest']):
                    raise ValueError('The stored source digest is invalid.')
                descriptors.append(PageDescriptor(expected, locator, row['digest']))
                previous = locator
            library._retained_source(db, workspace, identity, cancelled=budget.stopped)
            budget.check()
        self.identity = identity
        self.metadata = metadata
        self.pages = tuple(descriptors)

    @property
    def revision(self):
        return self.identity.revision

    @property
    def title(self):
        return self.metadata.title or 'Unknown title'

    @property
    def author(self):
        return self.metadata.author or 'Unknown author'

    def _header(self, db):
        if self.topic is not None:
            TopicTree(db, self.workspace)._visible(self.topic)
        edition = get_edition(Catalog(db, self.workspace), self.edition)
        row = db.execute(
            'SELECT r.id,r.asset FROM active a JOIN revision r '
            'ON r.id=a.revision AND r.workspace=a.workspace AND r.edition=a.edition '
            'JOIN sealed s ON s.revision=r.id WHERE a.workspace=? AND a.edition=?',
            (self.workspace, self.edition)).fetchone()
        if row is None:
            raise ValueError('This book has no active sealed text to read.')
        identity = Identity(self.workspace, self.edition, row['asset'], row['id'])
        if identity.asset_sha256 != edition.asset:
            raise ValueError('The source edition no longer matches its retained original.')
        row = db.execute(
            'SELECT author,title,volume,edition_label FROM source_metadata '
            'WHERE workspace=? AND edition=? AND revision=?',
            (self.workspace, self.edition, identity.revision)).fetchone()
        if row is None:
            raise ValueError('The source metadata is unavailable.')
        return identity, Metadata(*row)

    def page(self, ordinal):
        """Return one complete exact page; native verification is cancellable."""
        if type(ordinal) is not int or not 1 <= ordinal <= len(self.pages):
            raise ValueError('Choose a page or segment within this book.')
        expected = self.pages[ordinal - 1]
        with self.library._search_read(self.workspace, self.timeout, self.cancelled) as (db, budget):
            identity, metadata = self._header(db)
            if identity != self.identity or metadata != self.metadata:
                raise ValueError('The book revision changed. Reopen the reader; saved positions were not moved.')
            # Bound allocation before materializing raw text into Python or a
            # native text control. Oversized source segments remain available
            # through the existing external-original workflow.
            row = db.execute(
                'SELECT ordinal,physical,printed,verified,digest,length(raw) AS size,typeof(raw) AS kind '
                'FROM page WHERE revision=? AND ordinal=?', (self.revision, ordinal)).fetchone()
            if row is None or row['kind'] != 'text':
                raise ValueError('The selected source page is unavailable.')
            locator = _locator(row)
            if locator != expected.locator or row['digest'] != expected.text_sha256:
                raise ValueError('The source page identity changed. Reopen the reader.')
            if type(row['size']) is not int or not 0 <= row['size'] <= MAX_PAGE_CHARACTERS:
                raise ValueError('This page is too large for the reading window. Open the original source file.')
            raw = db.execute('SELECT raw FROM page WHERE revision=? AND ordinal=?',
                             (self.revision, ordinal)).fetchone()[0]
            if '\x00' in raw or digest(raw) != expected.text_sha256:
                raise ValueError('The stored source text has been damaged.')
            budget.check()
            _, mime, _ = self.library._retained_source(
                db, self.workspace, identity, cancelled=budget.stopped)
            _, _, attestor = self.library._loaders(db, self.workspace, cancelled=budget.stopped)
            verified = attestor(identity, locator, expected.text_sha256) is True
            budget.check()
            if mime == 'text/plain' and not verified:
                raise ValueError('The stored text does not match its original UTF-8 source.')
            location = ReadingLocation(self.workspace, self.edition, self.revision,
                                       locator.kind, locator.index, expected.text_sha256,
                                       identity.asset_sha256)
            return ReadingPage(location, locator if verified else _unapproved_locator(locator),
                               raw, metadata, verified, ordinal, len(self.pages))

    def validate(self, location):
        """Revalidate a page bookmark or resume position against its exact source."""
        if type(location) is not ReadingLocation or (
                location.workspace != self.workspace or location.edition != self.edition
                or location.revision != self.revision
                or location.asset_sha256 != self.identity.asset_sha256):
            raise ValueError('The saved position belongs to a different library, edition or revision.')
        descriptor = next((page for page in self.pages if
                           page.locator.kind == location.kind and page.locator.index == location.index), None)
        if descriptor is None or descriptor.text_sha256 != location.text_sha256:
            raise ValueError('The saved position no longer matches the source page.')
        page = self.page(descriptor.ordinal)
        if page.location != location:
            raise ValueError('The saved reading position changed.')
        return page
