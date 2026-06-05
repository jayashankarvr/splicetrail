"""Custom exception hierarchy for SpliceTrail."""


class SpliceTrailError(Exception):
    """Base exception for all SpliceTrail errors."""
    pass


class ImageError(SpliceTrailError):
    """Base exception for image-related errors."""
    pass


class InvalidImageError(ImageError):
    """Raised when an image is invalid or corrupted."""
    pass


class ImageFormatError(ImageError):
    """Raised when image format is unsupported."""
    pass


class ImageSizeError(ImageError):
    """Raised when image dimensions are invalid."""
    pass


class ConfigurationError(SpliceTrailError):
    """Raised when configuration parameters are invalid."""
    pass


class IndexError(SpliceTrailError):
    """Base exception for index-related errors."""
    pass


class IndexNotFoundError(IndexError):
    """Raised when trying to load a non-existent index."""
    pass


class IndexCorruptedError(IndexError):
    """Raised when index file is corrupted."""
    pass


class TargetNotFoundError(IndexError):
    """Raised when target image ID is not found in index."""
    pass


class TargetExistsError(IndexError):
    """Raised when trying to add a target that already exists."""
    pass


class SearchError(SpliceTrailError):
    """Base exception for search-related errors."""
    pass


class EmptyIndexError(SearchError):
    """Raised when searching an empty index."""
    pass


class VerificationError(SpliceTrailError):
    """Raised when patch verification fails."""
    pass


class DependencyError(SpliceTrailError):
    """Raised when optional dependency is missing."""
    pass
