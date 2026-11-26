import json
import sqlite3
import pytest
from .universal_library import ResearchLibrary


def test_universal(tmp_path):
    path=tmp_path/'library.db'; lib=ResearchLibrary(path)
    a=lib.workspace('History'); b=lib.workspace('Physics')
    c=lib.collection(a,'Sources'); d=lib.collection(a,'Other')
    t=lib.topic(a,'Question'); child=lib.topic(a,'Chapter',t)
    other=lib.topic(b,'Question')
    book=lib.add_book(a,'Same title','Author A','I',[('iv','A bridge across the river.'),(None,'Міст через річку.')])
    lib.add_book(b,'Same title','Author B','II',[(None,'A bridge across the river.')])
    lib.assign(a,c,book); lib.assign(a,d,book)
    assert len(lib.search(a,'bridge'))==1
    assert len(lib.search(b,'bridge'))==1
    assert lib.search(a,'bridge',c)[0]['author']=='Author A'
    assert lib.search(b,'bridge',c)==[]
    assert lib.search(a,'Міст')[0]['printed'] is None
    assert lib.search(a,'brid')==[]
    assert lib.search(a,'bridge river',thematic=True)
    hit=lib.search(a,'bridge')[0]; ev=lib.save(a,child,hit,'note')
    lib.note(a,ev,'updated')
    assert json.loads(lib.export_topic(a,child))[0]['note']=='updated'
    assert lib.export_topic(b,child)=='[]'
    with pytest.raises(ValueError): lib.save(b,other,hit)
    with pytest.raises(sqlite3.IntegrityError): lib.assign(b,c,book)
    with pytest.raises(ValueError): lib.move_topic(a,t,child)
    with pytest.raises(sqlite3.IntegrityError): lib.move_topic(a,child,other)
    with pytest.raises(ValueError): lib.save(a,child,{**hit,'quote':'invented'})
    with pytest.raises(sqlite3.IntegrityError): lib.db.execute('UPDATE page SET raw=? WHERE id=?',('tampered',hit['page']))
    lib.close(); lib=ResearchLibrary(path)
    assert json.loads(lib.export_topic(a,child))[0]['quote']==hit['quote']
    lib.close()


def test_real_format_import(tmp_path):
    from reportlab.pdfgen.canvas import Canvas
    lib=ResearchLibrary(tmp_path/'library.db'); w=lib.workspace('Independent collection')
    p=tmp_path/'book.pdf'; canvas=Canvas(str(p)); canvas.drawString(72,700,'Synthetic PDF: railway'); canvas.showPage(); canvas.drawString(72,700,'Synthetic PDF: ocean'); canvas.save()
    lib.import_file(w,p,'PDF sample','Fixture','1')
    h=lib.search(w,'ocean')[0]; assert h['physical']==2 and h['printed'] is None
    txt=tmp_path/'book.txt'; txt.write_text('First page\fSecond page: дослідження',encoding='utf-8')
    lib.import_file(w,txt,'Text sample','Fixture','2'); assert lib.search(w,'дослідження')[0]['physical']==2
    lib.close()
