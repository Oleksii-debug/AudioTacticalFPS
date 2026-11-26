"""Actual PDF extraction must not publish a partly omitted source as complete."""
from hashlib import sha256
import json

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

from carr_library.extract.page_ocr import Config
from carr_library.ingest.pipeline import Ingest
from carr_library.storage.revisions import Storage


def test_faint_scan_keeps_import_quarantined_after_reopen_and_retry(tmp_path):
    """Use real parsers; no OCR success or confidence can approve this page."""
    source = tmp_path / 'native-and-faint.pdf'
    scan = Image.new('RGB', (1200, 300), 'white')
    ImageDraw.Draw(scan).text(
        (30, 70), 'A faint original passage',
        font=ImageFont.load_default(size=65), fill=(250, 250, 250),
    )
    canvas = Canvas(str(source), pagesize=(612, 792))
    canvas.drawString(72, 700, 'A native original passage')
    canvas.showPage()
    canvas.drawImage(ImageReader(scan), 50, 500, 500, 125)
    canvas.showPage()
    canvas.save()
    before = sha256(source.read_bytes()).hexdigest()
    config = Config(dpi=144)

    storage = Storage(tmp_path / 'library.sqlite')
    ingest = Ingest(tmp_path / 'jobs.sqlite', storage)
    try:
        job = ingest.start('library', 'edition', tmp_path, source.name, config)
        first = ingest.run('library', job, tmp_path, source.name, config, max_steps=1)
        assert first['state'] == 'pending' and first['completed'] == 1
        final = ingest.run('library', job, tmp_path, source.name, config)
        assert final['state'] == 'quarantined'
        assert final['completed'] == final['total'] == 2
        assert storage.db.execute('SELECT COUNT(*) FROM active_page').fetchone()[0] == 0
        records = [json.loads(row[0]) for row in ingest.db.execute(
            'SELECT payload FROM checkpoint WHERE job=? ORDER BY ordinal', (job,),
        )]
        assert [record['physical_index'] for record in records] == [1, 2]
        assert records[0]['raw_text'] == 'A native original passage\n'
        assert records[1]['status'] == 'review_required'
        assert records[1]['provenance'].get('method') != 'render_blank'

        # The incomplete source remains incomplete after durable job recovery.
        ingest.close()
        ingest = Ingest(tmp_path / 'jobs.sqlite', storage)
        assert ingest.inspect('library', job)['state'] == 'quarantined'
        ingest.retry('library', job)
        assert ingest.inspect('library', job)['completed'] == 1
        assert ingest.run('library', job, tmp_path, source.name, config)['state'] == 'quarantined'
        assert storage.db.execute('SELECT COUNT(*) FROM active_page').fetchone()[0] == 0
        assert sha256(source.read_bytes()).hexdigest() == before
    finally:
        ingest.close()
        storage.close()
