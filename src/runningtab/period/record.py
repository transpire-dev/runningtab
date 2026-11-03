"""The record bucket: where a closed quarter's package lives once `period close` hands it off.

Storage under a retention policy (each books directory names its buckets in `books.toml`)
means an object, once written, cannot be overwritten or deleted until it ages
out. `Record` is the narrow surface that fact needs: list what is there, read
an object, create one -- and only ever create, never update. `GcsRecord` is
the real bucket; tests exercise the same functions against a plain dict, so
the revision numbering and read-back logic run with no credentials and no
network.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Protocol

from runningtab.period import PeriodError
from runningtab.period.manifest import lock_version_of
from runningtab.period.quarter import Quarter

# `<abbr>/<quarter>/SHA256SUMS` is revision 1; `<abbr>/<quarter>/revN/SHA256SUMS`
# is revision N. The abbr is stripped by the caller before matching, since
# `stored_packages` already scoped the listing to it.
_STORED_SUMS = re.compile(r"^(?P<quarter>\d{4}Q[1-4])/(?:rev(?P<rev>\d+)/)?SHA256SUMS$")


@dataclasses.dataclass(frozen=True)
class StoredPackage:
    """One complete package found in the bucket: which quarter, which revision, and where its objects sit."""

    quarter: Quarter
    revision: int
    prefix: str


class SuspectPackage(PeriodError):
    """`SHA256SUMS` was written but did not read back intact, so a package that may be bad is listed as complete.

    Every content file had already read back intact, but `stored_packages`
    now lists this prefix, and nothing here can delete it. A retry would
    only be refused as "already has a stored package", so the message names
    the prefix as suspect and sends the person to `runningtab period status`,
    which compares what is stored against the ledger.
    """


class Record(Protocol):
    """What the record bucket needs to offer. `GcsRecord` is the real one; tests use a plain dict."""

    def list_names(self, prefix: str) -> list[str]:
        """Every object name starting with `prefix`."""
        ...

    def read(self, name: str) -> bytes:
        """The object's whole content."""
        ...

    def create(self, name: str, data: bytes) -> None:
        """Write a new object. Raises `PeriodError` if `name` already exists -- this never overwrites."""
        ...


class GcsRecord:
    """`Record` against a real GCS bucket, under a retention policy or not.

    `google.cloud.storage` is imported here, inside the class, and nowhere
    else in this module -- so the unit suite, which only ever exercises a
    dict-backed `Record`, never needs credentials or reaches the network.
    """

    def __init__(self, bucket_name: str) -> None:
        from google.cloud import storage

        self.bucket_name = bucket_name
        self._client = storage.Client()
        self._bucket = self._client.bucket(bucket_name)

    def list_names(self, prefix: str) -> list[str]:
        return [blob.name for blob in self._client.list_blobs(self._bucket, prefix=prefix)]

    def read(self, name: str) -> bytes:
        return self._bucket.blob(name).download_as_bytes()

    def create(self, name: str, data: bytes) -> None:
        # `if_generation_match=0` -- generation 0 means "no live object of
        # this name" -- is the server-side precondition that makes this
        # never overwrite, even under a race with another writer.
        from google.api_core.exceptions import PreconditionFailed

        try:
            self._bucket.blob(name).upload_from_string(data, if_generation_match=0)
        except PreconditionFailed as exc:
            raise PeriodError(f"{name!r} already exists in {self.bucket_name!r}; refusing to overwrite") from exc


def stored_packages(record: Record, abbr: str) -> list[StoredPackage]:
    """Every complete package stored for `abbr`, found by locating each `SHA256SUMS` object.

    `SHA256SUMS` is the last object `upload_package` writes, so a package
    still being written -- or one that failed partway -- has no `SHA256SUMS`
    yet and is invisible here, exactly as it should be: nothing lists it as
    available until it is actually complete.
    """
    packages = []
    for name in record.list_names(f"{abbr}/"):
        match = _STORED_SUMS.fullmatch(name.removeprefix(f"{abbr}/"))
        if match is None:
            continue
        quarter = Quarter.parse(match["quarter"])
        revision = int(match["rev"]) if match["rev"] else 1
        prefix = f"{abbr}/{quarter.label}/" if revision == 1 else f"{abbr}/{quarter.label}/rev{revision}/"
        packages.append(StoredPackage(quarter, revision, prefix))
    return sorted(packages, key=lambda p: (p.quarter, p.revision))


def latest(stored: list[StoredPackage]) -> dict[Quarter, StoredPackage]:
    """The highest-revision `StoredPackage` for each quarter present in `stored`."""
    result: dict[Quarter, StoredPackage] = {}
    for package in stored:
        current = result.get(package.quarter)
        if current is None or package.revision > current.revision:
            result[package.quarter] = package
    return result


def _prefix_for(abbr: str, quarter: Quarter, revision: int) -> str:
    """`<abbr>/<quarter>/` for revision 1, `<abbr>/<quarter>/revN/` for any other -- the one place this format
    is spelled. `next_prefix` and `revision_of` are its only two callers, one building it and one reading it
    back, so the two can never drift apart the way a second, independent parse of the same shape could.
    """
    return f"{abbr}/{quarter.label}/" if revision == 1 else f"{abbr}/{quarter.label}/rev{revision}/"


def next_prefix(record: Record, abbr: str, quarter: Quarter) -> str:
    """Where the next revision of `quarter`'s package belongs.

    Probes each candidate prefix in turn -- `<abbr>/<quarter>/`, then
    `rev2/`, `rev3/`, ... -- and returns the first one holding no object at
    all. `stored_packages` calls a prefix with no `SHA256SUMS` "not there,"
    but a half-failed `upload_package` can still have left content objects
    under it, and this function must not repeat that prefix: the bucket is
    under a retention policy, those objects cannot be deleted until they age out, and a retry that
    recomputed the same prefix would have every `create` refuse against them
    forever. Checking `record.list_names` directly, rather than trusting
    `stored_packages`, is what makes a retry land on the next free revision
    instead.
    """
    revision = 1
    while True:
        prefix = _prefix_for(abbr, quarter, revision)
        if not record.list_names(prefix):
            return prefix
        revision += 1


_REVISION_SUFFIX = re.compile(r"rev(\d+)/$")


def revision_of(prefix: str, abbr: str, quarter: Quarter) -> int:
    """The revision `next_prefix(record, abbr, quarter)` encoded into a `prefix` it returned.

    A caller that renders `MANIFEST.md`'s revision field needs the exact
    number the prefix it is about to write under names -- this is the one
    place that reads the layout `next_prefix` writes, so the two can never
    silently disagree about what a bare prefix versus a `revN/` one means.
    """
    if prefix == _prefix_for(abbr, quarter, 1):
        return 1
    match = _REVISION_SUFFIX.search(prefix)
    if match is None or prefix != _prefix_for(abbr, quarter, int(match[1])):
        raise PeriodError(f"{prefix!r} is not a {abbr!r}/{quarter.label} revision prefix")
    return int(match[1])


def upload_package(record: Record, prefix: str, files: dict[str, bytes]) -> None:
    """Write every object in `files` under `prefix`, `SHA256SUMS` last, proving each one landed intact first.

    Every content file is created, then read back and checked before
    `SHA256SUMS` is ever written: a content file that fails its read-back
    raises immediately, with `SHA256SUMS` still unwritten, so a corrupt
    package never becomes visible as complete to `stored_packages`.
    `SHA256SUMS` itself is then created and read back the same way; a
    mismatch there raises `SuspectPackage`, since by then the package is
    already listed as complete. The
    read-back is not paranoia: a write GCS acknowledged is still only a
    claim about what is in the bucket, and a package an accountant trusts on
    a wrong claim is worse than one that fails loudly here instead.
    """
    content = {path: data for path, data in files.items() if path != "SHA256SUMS"}
    for path, data in content.items():
        record.create(f"{prefix}{path}", data)
    mismatched = [f"{prefix}{path}" for path, data in content.items() if record.read(f"{prefix}{path}") != data]
    if mismatched:
        raise PeriodError(f"uploaded but did not read back intact: {', '.join(sorted(mismatched))}")
    if "SHA256SUMS" in files:
        sums_name = f"{prefix}SHA256SUMS"
        record.create(sums_name, files["SHA256SUMS"])
        if record.read(sums_name) != files["SHA256SUMS"]:
            raise SuspectPackage(
                f"{sums_name} was written but did not read back intact, so {prefix!r} is now listed as a stored "
                f"package that may not be what was sent; treat {prefix!r} as suspect and run `runningtab period status` "
                "to compare it with the ledger"
            )


def read_lock_version(record: Record, stored: StoredPackage) -> str:
    """The Company Version a stored package's `MANIFEST.md` says locked its quarter.

    A stored package is always a final close, so a manifest with no lock
    record is refused by name rather than read as "no lock".
    """
    name = f"{stored.prefix}MANIFEST.md"
    try:
        text = record.read(name).decode()
    except UnicodeDecodeError as exc:
        raise PeriodError(f"{name} is not valid UTF-8: {exc}") from exc
    version = lock_version_of(text)
    if version is None:
        raise PeriodError(f"{name} records no lock (no Company Version line)")
    return version


def read_sums(record: Record, stored: StoredPackage) -> dict[str, str]:
    """`path -> hex digest`, parsed from the stored package's `SHA256SUMS` (the standard `sha256sum` two-space format).

    The bucket is under a retention policy and `upload_package` writes this object
    itself, so a malformed read is unlikely -- but unlikely is not never,
    and the ruling is that every read failure is a named refusal, not a
    traceback that loses whatever else `period status`/`health` already
    found.
    """
    name = f"{stored.prefix}SHA256SUMS"
    try:
        text = record.read(name).decode()
    except UnicodeDecodeError as exc:
        raise PeriodError(f"{name} is not valid UTF-8: {exc}") from exc
    result = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line:
            continue
        try:
            digest, path = line.split("  ", 1)
        except ValueError:
            raise PeriodError(f"{name} line {lineno} is not `<sha256>  <path>`: {line!r}") from None
        result[path] = digest
    return result
