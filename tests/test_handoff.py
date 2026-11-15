"""Handoff journal: batch registration, cursor paging and batch downloads."""

from __future__ import annotations

import hashlib
import io
import re
import sqlite3
from pathlib import Path

import pytest

from app import create_app
from app import handoff
from app.db import connect


@pytest.fixture()
def app(tmp_path: Path):
    application = create_app(
        {
            "TESTING": True,
            "DATA_DIR": str(tmp_path),
            "DATABASE": str(tmp_path / "test.db"),
            "FILES_DIR": str(tmp_path / "files"),
            "SECRET_KEY": "handoff-secret",
            "CSRF_ENABLED": False,
            "HANDOFF_PER_PAGE": 2,
        }
    )
    yield application


@pytest.fixture()
def client(app):
    return app.test_client()


def csv_bytes(rows: list[tuple[str, str, str, str, str]], header=None) -> bytes:
    header = header or "dataset_date,site,metric,observed_at,value"
    return ("\n".join([header, *[",".join(row) for row in rows]]) + "\n").encode()


def upload(client, rows, dataset_date="2026-11-14", name="obs.csv"):
    response = client.post(
        "/admin/upload",
        data={
            "dataset_date": dataset_date,
            "file": (io.BytesIO(csv_bytes(rows)), name),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 302, response.get_data(as_text=True)


def version_ids(app):
    conn = connect(app.config["DATABASE"])
    try:
        return [row["id"] for row in conn.execute("SELECT id FROM versions ORDER BY id")]
    finally:
        conn.close()


def journal(app):
    conn = connect(app.config["DATABASE"])
    try:
        return conn.execute(
            "SELECT * FROM publication_journal ORDER BY seq"
        ).fetchall()
    finally:
        conn.close()


def publish(client, version_id):
    return client.post(f"/admin/versions/{version_id}/publish")


def rows_for(day: str, value: str, site: str = "东闸断面"):
    return [(day, site, "氨氮", f"{day} 08:00:00", value)]


def publish_days(client, app, days):
    """Upload and publish one file per day; return version ids in order."""
    for day in days:
        upload(client, rows_for(day, "1.24"), dataset_date=day)
    ids = version_ids(app)
    for version_id in ids:
        publish(client, version_id)
    return ids


def start_round(client, cursor=0, per_page=2):
    response = client.post(
        "/admin/handoff",
        data={"cursor": str(cursor), "per_page": str(per_page)},
    )
    assert response.status_code == 302
    return response.headers["Location"]


def next_link(html: str) -> str | None:
    match = re.search(r'href="(/admin/handoff\?cursor=[^"]+)"[^>]*>下一页', html)
    return match.group(1).replace("&amp;", "&") if match else None


def page_cursors(html: str) -> list[int]:
    return [int(value) for value in re.findall(r'<td class="num mono">(\d+)</td>', html)]


def download(client, seq):
    return client.get(f"/admin/handoff/batches/{seq}/download")


# ---------------------------------------------------------------------------
# Registration is tied to a successful publish
# ---------------------------------------------------------------------------


def test_successful_publish_registers_one_batch(client, app):
    upload(client, rows_for("2026-11-14", "1.24"))
    version_id = version_ids(app)[0]
    publish(client, version_id)

    batches = journal(app)
    assert len(batches) == 1
    batch = batches[0]
    assert batch["version_id"] == version_id
    assert batch["dataset_date"] == "2026-11-14"
    assert batch["version_no"] == 1
    assert batch["row_count"] == 1
    assert batch["detail_sha256"]

    detail = download(client, batch["seq"])
    assert detail.status_code == 200
    assert detail.data.decode("utf-8-sig").splitlines() == [
        "dataset_date,site,metric,observed_at,value",
        "2026-11-14,东闸断面,氨氮,2026-11-14 08:00:00,1.24",
    ]


def test_repeated_publish_of_same_version_registers_nothing_new(client, app):
    upload(client, rows_for("2026-11-14", "1.24"))
    version_id = version_ids(app)[0]
    publish(client, version_id)
    first = [dict(row) for row in journal(app)]

    publish(client, version_id)
    publish(client, version_id)

    assert [dict(row) for row in journal(app)] == first


def test_failed_publish_registers_nothing(client, app):
    upload(client, rows_for("2026-11-14", "1.24"))
    stale = version_ids(app)[0]
    upload(client, rows_for("2026-11-14", "1.30"), name="newer.csv")
    newest = version_ids(app)[1]

    # Stale candidate: rejected as a conflict, no batch.
    publish(client, stale)
    assert journal(app) == []
    # Publish the newest, then retry the stale one: still no extra batch.
    publish(client, newest)
    publish(client, stale)
    assert len(journal(app)) == 1


def test_publish_blocked_by_missing_file_registers_nothing(client, app):
    upload(client, rows_for("2026-11-14", "1.24"))
    version_id = version_ids(app)[0]
    conn = connect(app.config["DATABASE"])
    stored_path = conn.execute(
        "SELECT stored_path FROM versions WHERE id = ?", (version_id,)
    ).fetchone()["stored_path"]
    conn.close()
    (Path(app.config["FILES_DIR"]) / stored_path).unlink()

    response = publish(client, version_id)
    assert response.status_code == 302
    assert journal(app) == []


def test_failed_journal_insert_rolls_back_the_whole_publish(client, app, monkeypatch):
    upload(client, rows_for("2026-11-14", "1.24"))
    version_id = version_ids(app)[0]

    real_journal_publish = handoff.journal_publish

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("simulated journal failure")

    from app import services

    # services imported journal_publish by name; patch it there.
    monkeypatch.setattr(services, "journal_publish", boom)

    conn = connect(app.config["DATABASE"])
    try:
        outcome = services.publish_candidate(
            conn, app.config["FILES_DIR"], version_id
        )
    finally:
        conn.close()

    assert outcome.status == "db_error"
    monkeypatch.setattr(services, "journal_publish", real_journal_publish)

    versions = connect(app.config["DATABASE"]).execute(
        "SELECT status, published_at FROM versions WHERE id = ?", (version_id,)
    ).fetchone()
    assert versions["status"] == "candidate"
    assert versions["published_at"] is None
    assert journal(app) == []
    assert client.get(f"/versions/{version_id}").status_code == 404


# ---------------------------------------------------------------------------
# Cursor paging
# ---------------------------------------------------------------------------


def test_round_fixes_cutoff_and_holds_new_publishes_for_next_round(client, app):
    ids = publish_days(client, app, ["2026-11-13", "2026-11-14"])
    assert len(ids) == 2

    location = start_round(client, cursor=0, per_page=1)
    html = client.get(location).get_data(as_text=True)
    assert page_cursors(html) == [1]
    assert "本轮截止批次" in html

    # A publish while the round is being read must not enter this round.
    upload(client, rows_for("2026-11-15", "0.86"), dataset_date="2026-11-15")
    publish(client, version_ids(app)[2])
    assert len(journal(app)) == 3

    second = client.get(next_link(html))
    html2 = second.get_data(as_text=True)
    assert page_cursors(html2) == [2]
    assert 'id="round-complete"' in html2
    # Batch 3 belongs to the next round.
    assert "本轮之外的新发布" in html2
    assert "<dd>1 批（留到下一轮）</dd>" in html2

    # Restarting from the completed cursor picks batch 3 up.
    html3 = client.get(start_round(client, cursor=2, per_page=5)).get_data(as_text=True)
    assert page_cursors(html3) == [3]


def test_retrying_same_continue_cursor_returns_same_batches(client, app):
    publish_days(client, app, ["2026-11-13", "2026-11-14", "2026-11-15"])
    location = start_round(client, cursor=0, per_page=2)
    first = client.get(location).get_data(as_text=True)
    token_url = next_link(first)
    assert token_url

    a = client.get(token_url).get_data(as_text=True)
    b = client.get(token_url).get_data(as_text=True)
    assert page_cursors(a) == page_cursors(b) == [3]
    assert a == b

    # Even after a new publish, the same continue cursor is unchanged.
    upload(client, rows_for("2026-11-16", "2.00"), dataset_date="2026-11-16")
    publish(client, version_ids(app)[3])
    c = client.get(token_url).get_data(as_text=True)
    assert page_cursors(c) == [3]
    assert 'id="round-complete"' in c


def test_round_pages_cover_every_batch_once_in_order(client, app):
    days = ["2026-11-13", "2026-11-14", "2026-11-15", "2026-11-16", "2026-11-17"]
    publish_days(client, app, days)

    location = start_round(client, cursor=0, per_page=2)
    seen: list[int] = []
    while location:
        html = client.get(location).get_data(as_text=True)
        seen.extend(page_cursors(html))
        if 'id="round-complete"' in html:
            break
        location = next_link(html)
    assert seen == [1, 2, 3, 4, 5]


def test_resume_cursor_from_middle_skips_nothing_before_and_nothing_after(client, app):
    publish_days(client, app, ["2026-11-13", "2026-11-14", "2026-11-15"])
    location = start_round(client, cursor=2, per_page=5)
    html = client.get(location).get_data(as_text=True)
    assert page_cursors(html) == [3]
    assert 'id="round-complete"' in html


def test_cursor_outside_journal_is_rejected(client, app):
    publish_days(client, app, ["2026-11-13"])
    response = client.post(
        "/admin/handoff", data={"cursor": "99"}, follow_redirects=True
    )
    assert "超过当前最新批次" in response.get_data(as_text=True)

    response = client.post(
        "/admin/handoff", data={"cursor": "7"}, follow_redirects=True
    )
    assert "超过当前最新批次" in response.get_data(as_text=True)

    response = client.post(
        "/admin/handoff", data={"cursor": "-1"}, follow_redirects=True
    )
    assert "非负整数" in response.get_data(as_text=True)


def test_tampered_cursor_is_refused(client, app):
    publish_days(client, app, ["2026-11-13", "2026-11-14"])
    location = start_round(client, cursor=0, per_page=2)
    token = location.split("cursor=", 1)[1]

    tampered = token[:-2] + ("AA" if not token.endswith("AA") else "BB")
    response = client.get(
        "/admin/handoff", query_string={"cursor": tampered}, follow_redirects=True
    )
    body = response.get_data(as_text=True)
    assert "续传游标无效" in body
    assert "本轮批次总数" not in body


def test_cursor_tokens_are_bound_to_the_app_secret():
    cursor = handoff.HandoffCursor(after=3, cutoff=9, per_page=2, round_start=1)
    token = handoff.encode_cursor(cursor, secret="one")
    assert handoff.decode_cursor(token, secret="one") == cursor
    assert handoff.decode_cursor(token, secret="two") is None
    assert handoff.decode_cursor("", secret="one") is None
    assert handoff.decode_cursor("not-a-token", secret="one") is None


def test_per_page_is_clamped():
    assert handoff.clamp_per_page(None) == handoff.DEFAULT_BATCHES_PER_PAGE
    assert handoff.clamp_per_page(0) == 1
    assert handoff.clamp_per_page(1_000_000) == handoff.MAX_BATCHES_PER_PAGE
    assert handoff.clamp_per_page("abc") == handoff.DEFAULT_BATCHES_PER_PAGE


# ---------------------------------------------------------------------------
# Batch content is pinned to its version
# ---------------------------------------------------------------------------


def test_re_publish_creates_new_batch_and_keeps_old_batch_content(client, app):
    publish_days(client, app, ["2026-11-14"])
    first = journal(app)[0]
    before = download(client, first["seq"]).data

    upload(
        client,
        rows_for("2026-11-14", "1.99") + [
            ("2026-11-14", "西渡断面", "pH", "2026-11-14 09:00:00", "7.1")
        ],
        name="rerun.csv",
    )
    publish(client, version_ids(app)[1])

    batches = journal(app)
    assert [batch["seq"] for batch in batches] == [1, 2]
    assert batches[1]["version_id"] != first["version_id"]
    assert download(client, first["seq"]).data == before
    after = download(client, batches[1]["seq"]).data.decode("utf-8-sig")
    assert "1.99" in after and "7.1" in after
    assert "1.24" not in after


def test_batch_download_cannot_mix_versions_on_the_same_date(client, app):
    publish_days(client, app, ["2026-11-14", "2026-11-15"])
    # Re-publish 2026-11-14: v2 for that date supersedes v1.
    upload(client, rows_for("2026-11-14", "3.00"), name="again.csv")
    publish(client, version_ids(app)[2])

    batches = journal(app)
    assert [batch["dataset_date"] for batch in batches] == [
        "2026-11-14",
        "2026-11-15",
        "2026-11-14",
    ]
    for batch in batches:
        body = download(client, batch["seq"]).data.decode("utf-8-sig")
        dates = {line.split(",")[0] for line in body.splitlines()[1:]}
        assert dates == {batch["dataset_date"]}
        version_ids_in_body = {line.split(",")[1] for line in body.splitlines()[1:]}
        assert version_ids_in_body  # every download has at least one detail row
    assert download(client, 1).data != download(client, 3).data


def test_journal_digest_mismatch_blocks_download(client, app):
    publish_days(client, app, ["2026-11-14"])
    seq = journal(app)[0]["seq"]

    conn = connect(app.config["DATABASE"])
    try:
        # Simulate the batch's observation set changing after handoff; the
        # append-only journal itself is untouched.
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute(
            "UPDATE observations SET value_text = '9.99' WHERE version_id = ("
            "SELECT version_id FROM publication_journal WHERE seq = ?)",
            (seq,),
        )
    finally:
        conn.close()

    response = download(client, seq)
    assert response.status_code == 503
    assert "不一致" in response.get_data(as_text=True)


def test_journal_rows_cannot_be_updated_or_deleted(client, app):
    publish_days(client, app, ["2026-11-14"])
    conn = connect(app.config["DATABASE"])
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE publication_journal SET version_no = 99")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM publication_journal")
    finally:
        conn.close()
    assert len(journal(app)) == 1


def test_batch_download_headers_and_missing_batch(client, app):
    publish_days(client, app, ["2026-11-14"])
    batch = journal(app)[0]
    response = download(client, batch["seq"])
    assert response.headers["X-Handoff-Seq"] == str(batch["seq"])
    assert response.headers["X-Handoff-Version-Id"] == str(batch["version_id"])
    assert response.headers["X-Handoff-Detail-Sha256"] == batch["detail_sha256"]
    assert (
        hashlib.sha256(response.data).hexdigest() == batch["detail_sha256"]
    )
    assert "water_quality_2026-11-14_v1_batch1.csv" in response.headers[
        "Content-Disposition"
    ]
    assert download(client, 999).status_code == 404


def test_handoff_requires_csrf_when_enabled(tmp_path):
    application = create_app(
        {
            "DATA_DIR": str(tmp_path),
            "DATABASE": str(tmp_path / "t.db"),
            "FILES_DIR": str(tmp_path / "files"),
            "SECRET_KEY": "s",
        }
    )
    response = application.test_client().post("/admin/handoff", data={"cursor": "0"})
    assert response.status_code == 400


def test_empty_journal_round_is_a_clean_no_op(client, app):
    location = start_round(client, cursor=0)
    html = client.get(location).get_data(as_text=True)
    assert "本轮没有新批次" in html
    assert 'id="round-complete"' in html
    assert "<dd class=\"mono\"><strong>0</strong></dd>" in html


def test_continue_cursor_from_a_restored_backup_is_refused(client, app):
    publish_days(client, app, ["2026-11-13", "2026-11-14"])
    location = start_round(client, cursor=0, per_page=2)
    token = location.split("cursor=", 1)[1]

    # Simulate a restored older database: the cursor claims batch 2 exists
    # while only batch 1 is present.
    conn = connect(app.config["DATABASE"])
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DROP TRIGGER publication_journal_no_delete")
        conn.execute("DELETE FROM publication_journal WHERE seq = 2")
    finally:
        conn.close()

    response = client.get(
        "/admin/handoff", query_string={"cursor": token}, follow_redirects=True
    )
    body = response.get_data(as_text=True)
    assert "只有 1 个批次" in body
    assert 'id="round-complete"' not in body
