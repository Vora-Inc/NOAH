"""Legacy import path; the shared publication lock is not a pool feature."""
from ..core.publication_lock import deployment_lease, publication_lock, serialized  # noqa: F401
