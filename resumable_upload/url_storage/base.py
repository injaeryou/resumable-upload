"""URL storage abstract base class."""

from abc import ABC, abstractmethod
from typing import Optional


class URLStorage(ABC):
    """Abstract interface for URL storage implementations."""

    @abstractmethod
    def get_url(self, fingerprint: str) -> Optional[str]:
        """
        Retrieve upload URL for a given file fingerprint.

        Args:
            fingerprint: Unique file fingerprint

        Returns:
            Upload URL if found, None otherwise
        """
        pass

    @abstractmethod
    def set_url(self, fingerprint: str, url: str) -> None:
        """
        Store upload URL for a given file fingerprint.

        Args:
            fingerprint: Unique file fingerprint
            url: Upload URL to store
        """
        pass

    @abstractmethod
    def remove_url(self, fingerprint: str) -> None:
        """
        Remove stored URL for a given file fingerprint.

        Args:
            fingerprint: Unique file fingerprint
        """
        pass
