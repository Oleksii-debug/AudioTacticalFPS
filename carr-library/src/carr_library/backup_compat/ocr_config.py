"""Validate legacy OCR declarations without discovering or loading an engine."""
from dataclasses import dataclass

from .ocr import split_languages

POLICY = 'tessdata-fast-87416418657359cb625c412a48b6e1d6d41c29bd'
PINNED = frozenset(('eng', 'ukr', 'slk'))


@dataclass(frozen=True)
class Config:
    languages: str = 'eng'
    dpi: int = 300
    timeout: int = 30
    max_pixels: int = 20_000_000
    orientation: bool = False
    model_policy: str = 'default'
    model_profile: str = 'installed'

    def __post_init__(self):
        selected = split_languages(self.languages)
        if type(self.orientation) is not bool:
            raise ValueError('Invalid legacy OCR orientation flag.')
        if self.model_policy not in ('default', POLICY) or self.model_profile not in ('installed', 'fast', 'best'):
            raise ValueError('Unsupported legacy OCR declaration.')
        if self.model_policy == POLICY and (self.model_profile == 'best' or any(code not in PINNED for code in selected)):
            raise ValueError('Legacy OCR pinned policy conflicts with its model selection.')
        if (any(type(value) is not int for value in (self.dpi, self.timeout, self.max_pixels))
                or not 72 <= self.dpi <= 400 or not 1 <= self.timeout <= 120
                or not 1 <= self.max_pixels <= 20_000_000):
            raise ValueError('Invalid legacy OCR resource limit.')
