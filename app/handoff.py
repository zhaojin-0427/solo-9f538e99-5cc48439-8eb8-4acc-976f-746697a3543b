"""Incremental handoff journal: cursor paging and normalized batch exports.

A publish appends exactly one journalled batch, in the same transaction that
makes the version visible.  Downstream readers resume from the cursor they
completed last.  Starting a round fixes an upper bound (the *cutoff* batch)
once; batches published while the round is being read carry a higher
``seq`` and are therefore held back for the next round.

Paging addresses batches by ``seq`` only (``seq > after AND seq <= cutoff``)
and carries the whole window inside the *continue cursor* token returned by
each page.  Retrying that token therefore always describes the same batch
window: no batch can be skipped or repeated, and no batch's observations can
be mixed with another version's.
"""

from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import hmac
import io
import sqlite3
from dataclasses import dataclass

CSV_HEADER = ("dataset_date", "site", "metric", "observed_at", "value")
DEFAULT_BATCHES_PER_PAGE = 10
MAX_BATCHES_PER_PAGE = 100
_TOKEN_VERSION = "h1"
_MAC_SIZE = 16


@dataclass(frozen=True)
class HandoffCursor:
    """A fixed window over the journal.

    ``after`` is the exclusive lower bound (the last batch already handed
    over), ``cutoff`` is the round's inclusive upper bound, and
    ``round_start`` is the cursor the round began from (used for progress
    display only).
    """

    after: int
    cutoff: int
    per_page: int
    round_start: int


@dataclass
class HandoffPage:
    batches: list[sqlite3.Row]
    cursor: HandoffCursor
    has_more: bool
    next_cursor: str | None
    remaining: int
    total_in_round: int
    completed_cursor: int | None

    @property
    def ended(self) -> bool:
        return not self.has_more

    @property
    def last_cursor(self) -> int:
        return int(self.batches[-1]["seq"]) if self.batches else self.cursor.after

    @property
    def done_in_round(self) -> int:
        return self.total_in_round - self.remaining


def clamp_per_page(value: int | None) -> int:
    try:
        number = int(value) if value is not None else DEFAULT_BATCHES_PER_PAGE
    except (TypeError, ValueError):
        number = DEFAULT_BATCHES_PER_PAGE
    return max(1, min(number, MAX_BATCHES_PER_PAGE))


def _key(secret: str | bytes) -> bytes:
    return secret.encode("utf-8") if isinstance(secret, str) else secret


def encode_cursor(cursor: HandoffCursor, *, secret: str | bytes) -> str:
    """Return an opaque, tamper-evident continue cursor."""
    payload = (
        f"{_TOKEN_VERSION}|{cursor.after}|{cursor.cutoff}|"
        f"{cursor.per_page}|{cursor.round_start}"
    ).encode("ascii")
    mac = hmac.new(_key(secret), payload, hashlib.sha256).digest()[:_MAC_SIZE]
    raw = payload + mac
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(token: str, *, secret: str | bytes) -> HandoffCursor | None:
    """Decode a continue cursor, or return ``None`` if it is not ours."""
    token = (token or "").strip()
    if not token or len(token) > 256:
        return None
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except (binascii.Error, ValueError):
        return None
    if len(raw) <= _MAC_SIZE:
        return None
    payload, supplied_mac = raw[:-_MAC_SIZE], raw[-_MAC_SIZE:]
    expected_mac = hmac.new(_key(secret), payload, hashlib.sha256).digest()[:_MAC_SIZE]
    if not hmac.compare_digest(supplied_mac, expected_mac):
        return None
    try:
        version, after, cutoff, per_page, round_start = payload.decode("ascii").split("|")
        after, cutoff = int(after), int(cutoff)
        per_page, round_start = int(per_page), int(round_start)
    except (UnicodeDecodeError, ValueError):
        return None
    if version != _TOKEN_VERSION:
        return None
    if after < 0 or cutoff < 0 or round_start < 0 or round_start > after:
        return None
    if after > cutoff or not 1 <= per_page <= MAX_BATCHES_PER_PAGE:
        return None
    return HandoffCursor(
        after=after, cutoff=cutoff, per_page=per_page, round_start=round_start
    )


def latest_seq(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) AS latest FROM publication_journal"
    ).fetchone()
    return int(row["latest"])


def count_after(conn: sqlite3.Connection, cutoff: int) -> int:
    """Number of batches published strictly after the round's cutoff."""
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM publication_journal WHERE seq > ?", (cutoff,)
        ).fetchone()[0]
    )


def count_batches(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute("SELECT COUNT(*) FROM publication_journal").fetchone()[0]
    )


def fetch_page(
    conn: sqlite3.Connection, cursor: HandoffCursor, *, secret: str | bytes
) -> HandoffPage:
    """Fetch complete batches strictly after ``cursor.after`` up to cutoff."""
    rows = conn.execute(
        """
        SELECT * FROM publication_journal
         WHERE seq > ? AND seq <= ?
         ORDER BY seq ASC
         LIMIT ?
        """,
        (cursor.after, cursor.cutoff, cursor.per_page + 1),
    ).fetchall()
    has_more = len(rows) > cursor.per_page
    batches = rows[: cursor.per_page]

    next_cursor = None
    if has_more:
        next_cursor = encode_cursor(
            HandoffCursor(
                after=int(batches[-1]["seq"]),
                cutoff=cursor.cutoff,
                per_page=cursor.per_page,
                round_start=cursor.round_start,
            ),
            secret=secret,
        )

    remaining_after = int(batches[-1]["seq"]) if batches else cursor.after
    remaining = int(
        conn.execute(
            "SELECT COUNT(*) FROM publication_journal WHERE seq > ? AND seq <= ?",
            (remaining_after, cursor.cutoff),
        ).fetchone()[0]
    )
    total_in_round = int(
        conn.execute(
            "SELECT COUNT(*) FROM publication_journal WHERE seq > ? AND seq <= ?",
            (cursor.round_start, cursor.cutoff),
        ).fetchone()[0]
    )

    return HandoffPage(
        batches=batches,
        cursor=cursor,
        has_more=has_more,
        next_cursor=next_cursor,
        remaining=remaining,
        total_in_round=total_in_round,
        completed_cursor=cursor.cutoff if not has_more else None,
    )


def get_batch(conn: sqlite3.Connection, seq: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM publication_journal WHERE seq = ?", (seq,)
    ).fetchone()


def detail_csv_bytes(
    conn: sqlite3.Connection, *, version_id: int, dataset_date: str
) -> bytes:
    """Render the normalized batch detail for exactly one version.

    Only observations of ``version_id`` are read, so a batch can never mix
    versions.  The output is deterministic for a given version, which lets
    the publish transaction record its digest as an immutability witness.
    """
    rows = conn.execute(
        """
        SELECT site, metric, observed_at, value_text
          FROM observations
         WHERE version_id = ?
         ORDER BY site, metric, observed_at, row_no
        """,
        (version_id,),
    ).fetchall()
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(CSV_HEADER)
    for row in rows:
        writer.writerow(
            (
                dataset_date,
                row["site"],
                row["metric"],
                row["observed_at"],
                row["value_text"],
            )
        )
    return buffer.getvalue().encode("utf-8")


def journal_publish(
    conn: sqlite3.Connection,
    *,
    version_id: int,
    dataset_date: str,
    version_no: int,
    sha256: str,
    published_at: str,
) -> int:
    """Append one batch to the journal; must run inside the publish txn.

    The batch is bound to the exact version and observation set that are
    being published, and records the digest of that set.  It is only ever
    inserted (the table rejects UPDATE/DELETE), so a later re-publish of the
    same date appends a new batch and leaves this one byte-for-byte intact.
    """
    detail = detail_csv_bytes(
        conn, version_id=version_id, dataset_date=dataset_date
    )
    count_row = conn.execute(
        "SELECT COUNT(*) AS n FROM observations WHERE version_id = ?",
        (version_id,),
    ).fetchone()
    row_count = int(count_row["n"])
    version_row = conn.execute(
        "SELECT row_count FROM versions WHERE id = ?", (version_id,)
    ).fetchone()
    if version_row is None:
        raise sqlite3.IntegrityError("版本记录不存在，无法登记交接批次")
    if int(version_row["row_count"]) != row_count:
        raise sqlite3.IntegrityError("明细条数与版本记录不一致，拒绝登记批次")
    cursor = conn.execute(
        """
        INSERT INTO publication_journal (
            version_id, dataset_date, version_no, row_count,
            sha256, detail_sha256, published_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            version_id,
            dataset_date,
            version_no,
            row_count,
            sha256,
            hashlib.sha256(detail).hexdigest(),
            published_at,
        ),
    )
    return int(cursor.lastrowid)
