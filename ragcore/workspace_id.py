"""A stable data-directory identifier shared with the dependency-free launcher."""
import hashlib
import os
from pathlib import Path


def workspace_id(directory):
    canonical = os.path.normcase(str(Path(directory).resolve()))
    return hashlib.sha256(os.fsencode(canonical)).hexdigest()
