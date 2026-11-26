import sqlite3
import hashlib
import unittest
from carr_library.search import literal

class FilterContract(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE active_page(id,workspace,edition,revision,asset,ordinal,physical,printed,verified,raw,digest)')
        for i,(ws,ed) in enumerate([('w','a'),('w','b'),('w','ab'),('other','ab')],1):
            raw='Exact quotation.'
            self.db.execute('INSERT INTO active_page VALUES(?,?,?,?,?,?,?,?,?,?,?)',(i,ws,ed,'r'+str(i),'a'*64,1,1,'1',1,raw,hashlib.sha256(raw.encode()).hexdigest()))
    def test_explicit_filter(self):
        self.assertEqual([h['edition_id'] for h in literal.search(self.db,'w','quotation',edition_ids=['ab'])],['ab'])
    def test_scalar_filter_rejected(self):
        with self.assertRaises(ValueError):
            literal.search(self.db,'w','quotation',edition_ids='ab')
    def test_oversized_filter_consumption(self):
        count=0
        def ids():
            nonlocal count
            for i in range(3000):
                count+=1
                yield str(i)
        with self.assertRaises(ValueError):
            literal.search(self.db,'w','quotation',edition_ids=ids())
        self.assertLessEqual(count,501)
    def test_cancellation_during_filter(self):
        count=0
        def ids():
            nonlocal count
            for i in range(300):
                count+=1
                yield str(i)
        with self.assertRaises(TimeoutError):
            literal.search(self.db,'w','quotation',edition_ids=ids(),cancelled=lambda: count>=5)
        self.assertLessEqual(count,6)
    def test_empty_filter(self):
        self.assertEqual(literal.search(self.db,'w','quotation',edition_ids=[]),[])
    def test_generator_filter(self):
        self.assertEqual([h['edition_id'] for h in literal.search(self.db,'w','quotation',edition_ids=iter(['ab']))],['ab'])
    def test_500_ids(self):
        ids=['ab']+[str(i) for i in range(499)]
        self.assertEqual([h['edition_id'] for h in literal.search(self.db,'w','quotation',edition_ids=ids)],['ab'])
    def test_invalid_element(self):
        with self.assertRaises(ValueError):
            literal.search(self.db,'w','quotation',edition_ids=['ab',None])

if __name__=='__main__':unittest.main()
