class ShareQuantError(Exception):
    """Base project exception."""


class MissingTokenError(ShareQuantError):
    """Raised when a real Tushare request is attempted without a token."""


class DatasetNotFoundError(ShareQuantError):
    """Raised when an unknown dataset name is requested."""

