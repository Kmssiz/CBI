"""Static files storage for production."""
from whitenoise.storage import CompressedManifestStaticFilesStorage


class TolerantManifestStorage(CompressedManifestStaticFilesStorage):
    """
    Hashed, compressed static files that tolerate dead references.

    The vendored theme CSS points at a few images that were never shipped. The
    stock manifest storage aborts ``collectstatic`` on them, which would stop the
    container from starting; here the dead reference is simply left as it is
    (it was already a 404).
    """

    manifest_strict = False

    def hashed_name(self, name, content=None, filename=None):
        try:
            return super().hashed_name(name, content, filename)
        except ValueError:
            return name
