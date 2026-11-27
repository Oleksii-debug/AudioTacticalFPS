"""Bounded native search-page navigation; source authorization stays in services."""
from collections import deque


class SearchNavigation:
    def __init__(self):
        self.history = deque(maxlen=128)
        self.cursor = None
        self.next_cursor = None
        self.number = 0

    def reset(self):
        self.history.clear()
        self.cursor = self.next_cursor = None
        self.number = 0

    def target(self, direction):
        if direction == 'first':
            return None
        if direction == 'next' and self.next_cursor is not None:
            return self.next_cursor
        if direction == 'previous' and self.history:
            return self.history[-1]
        raise ValueError('That search page is unavailable. Start a new search.')

    def accept(self, direction, used_cursor, next_cursor):
        if self.target(direction) != used_cursor:
            raise ValueError('Search navigation changed before completion.')
        if direction == 'first':
            self.reset()
            self.number = 1
        elif direction == 'next':
            self.history.append(self.cursor)
            self.number += 1
        else:
            self.history.pop()
            self.number -= 1
        self.cursor, self.next_cursor = used_cursor, next_cursor
