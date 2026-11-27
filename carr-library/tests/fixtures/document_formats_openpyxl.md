# Original synthetic XLSX fixture

`document_formats_openpyxl.xlsx` was generated locally with openpyxl 3.1.5.
It contains no imported/user material, macros, external links or credentials.
Size: 5385 bytes. SHA256:
`395bb823770b208d4863d02ea6279cf1f30b47d22927b8bf07ae5183cb911e12`.

Generation:

```python
from openpyxl import Workbook

book = Workbook()
book.active.title = 'Дослідження'
book.active.append(['Українська', 'Slovenčina', 42])
book.create_sheet('Later').append(['Last'])
book.save('document_formats_openpyxl.xlsx')
```

ZIP timestamps mean regenerating the workbook does not reproduce its exact
archive hash. The retained fixture preserves the actual output from the
generator; acceptance parses those fixed bytes with the production extractor.
No openpyxl installation is needed to run the test or import an XLSX document.
