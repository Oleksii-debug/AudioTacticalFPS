"""Bounded cursor-only navigation; result text and source handles are not cached."""
from collections import deque

from ..search.pagination import MAX_CURSOR_BYTES


class PageHistory:
    def __init__(self, *, max_bytes=1024 * 1024):
        if type(max_bytes) is not int or not MAX_CURSOR_BYTES <= max_bytes <= 64 * 1024 * 1024:
            raise ValueError('Invalid page-history metadata budget.')
        self.max_bytes = max_bytes
        self.reset()

    def reset(self):
        self.previous = deque()
        self.bytes = 0
        self.current_cursor = self.next_cursor = None
        self.page_number = 0
        self.expired = False

    @staticmethod
    def _size(cursor):
        if cursor is None:
            return 1
        if not isinstance(cursor, str) or len(cursor) > MAX_CURSOR_BYTES:
            raise ValueError('Invalid bounded page cursor.')
        return len(cursor.encode('ascii')) + 64

    @property
    def can_previous(self):
        return bool(self.previous)

    @property
    def can_next(self):
        return self.next_cursor is not None

    def request(self, direction):
        if direction == 'first':
            return None
        if direction == 'next' and self.can_next:
            return self.next_cursor
        if direction == 'previous' and self.can_previous:
            return self.previous[-1]
        raise ValueError('That search page is unavailable.')

    def accept(self, direction, requested, next_cursor, page_number):
        if requested != self.request(direction):
            raise ValueError('The requested search page changed.')
        self._size(next_cursor)
        if type(page_number) is not int or page_number < 1:
            raise ValueError('Invalid completed search page.')
        if direction == 'first':
            self.reset()
        elif direction == 'previous':
            self.bytes -= self._size(self.previous.pop())
        else:
            self.previous.append(self.current_cursor)
            self.bytes += self._size(self.current_cursor)
            while self.bytes > self.max_bytes:
                self.bytes -= self._size(self.previous.popleft())
                self.expired = True
        self.current_cursor, self.next_cursor = requested, next_cursor
        self.page_number = page_number

    @property
    def history_notice(self):
        return (' Earlier navigation cursors expired from the bounded history; '
                'start the search again to revisit them.' if self.expired else '')
