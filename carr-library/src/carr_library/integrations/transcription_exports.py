"""Publication outcomes shared by both existing transcription protocols."""


class TranscriptionExportedError(RuntimeError):
    """Some output is published, or a completed publication needs inspection.

    The original recognition job is unchanged. Callers must show this receipt
    instead of describing the operation as an unpublished cancellation or
    automatically retrying into the same destination.
    """

    def __init__(self, receipt, *, durability_confirmed=False):
        self.receipt = receipt
        self.destination = receipt.get('path', receipt.get('directory'))
        self.durability_confirmed = bool(durability_confirmed)
        self.publication_status = receipt['status']
        if self.publication_status == 'published':
            detail = ('the complete export is published; temporary cleanup needs review'
                      if self.durability_confirmed else
                      'the complete export is published; final directory durability is unconfirmed')
        elif self.publication_status == 'partial':
            detail = 'some files are published, but the complete export was not published'
        else:
            detail = 'publication of at least one file is uncertain; inspect the recorded paths'
        super().__init__('Transcription export at ' + str(self.destination) + ': ' + detail +
                         '. Existing outputs were retained. Review them before choosing a new export directory.')
