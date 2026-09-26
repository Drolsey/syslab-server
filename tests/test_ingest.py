"""The ingestion contract, sub-step 2.0.

The gate for this sub-step, from `docs/plans/step-02-ingestion-contract.md`:
a fake producer runs, records, invalidates on size, mtime and version change,
and its failure is recorded rather than raised.

Everything here uses a fake producer on purpose. Sub-step 2.1 moves the real
text extraction behind this interface, and the whole value of doing it in that
order is that the pipeline is already proven when the first real producer
arrives -- so a change in `check_search`'s numbers then means the move broke
something, rather than meaning the pipeline was never right.
"""

from __future__ import annotations

import os

import pytest

from app import config, context, ingest
from tests.conftest import OTHER_TENANT


@pytest.fixture(autouse=True)
def a_pipeline_with_nothing_in_it():
    """No test inherits another's producers.

    The registry is module state, so a producer registered by one test is in
    the pipeline for every test after it -- which shows up as a manifest row
    nobody wrote and is miserable to trace back.
    """
    saved = ingest.registered()
    ingest._PRODUCERS.clear()
    yield
    ingest._PRODUCERS.clear()
    ingest._PRODUCERS.update(saved)


def a_file(folder, name="report.txt", body="original"):
    path = folder / name
    path.write_text(body, encoding="utf-8")
    return path


def counting_producer(name="fake", version=1, slow=False, handles=(".txt",)):
    """A producer that writes one file and counts how often it was asked to."""
    calls = []

    def run(source, out_dir):
        calls.append(source.name)
        made = out_dir / "output.txt"
        made.write_text(source.read_text(encoding="utf-8").upper(), encoding="utf-8")
        return ingest.Result.made(made.relative_to(config.derived_dir()))

    producer = ingest.register(ingest.Producer(
        name=name, version=version, handles=frozenset(handles), slow=slow, run=run,
    ))
    return producer, calls


# --------------------------------------------------------------------------
# it runs, and it records
# --------------------------------------------------------------------------

def test_a_producer_runs_and_its_work_is_recorded(tenant_storage):
    _, calls = counting_producer()
    a_file(tenant_storage)

    report = ingest.ingest("report.txt")

    assert calls == ["report.txt"]
    assert report["ran"] == ["fake"]
    assert report["failed"] == {}

    row = ingest.status("report.txt")["producers"]["fake"]
    assert row["status"] == ingest.OK
    assert row["attempts"] == 1
    assert row["stale"] is False
    # Forward slashes whatever platform wrote it: the same code runs on this
    # laptop and on the Ubuntu box, and a path is the one field here that
    # another machine might have to read.
    assert row["output_path"] == "fake/report.txt/output.txt"
    assert (config.derived_dir() / row["output_path"]).read_text(encoding="utf-8") == "ORIGINAL"


def test_a_second_pass_over_an_unchanged_file_runs_nothing(tenant_storage):
    _, calls = counting_producer()
    a_file(tenant_storage)

    ingest.ingest("report.txt")
    report = ingest.ingest("report.txt")

    assert calls == ["report.txt"], "the producer ran again for a file that did not change"
    assert report["current"] == ["fake"]
    assert report["ran"] == []


def test_a_document_is_ready_only_when_every_producer_has_current_work(tenant_storage):
    counting_producer(name="text")
    counting_producer(name="images")
    a_file(tenant_storage)

    assert ingest.status("report.txt")["ready"] is False
    assert sorted(ingest.needs("report.txt")) == ["images", "text"]

    ingest.ingest("report.txt")

    assert ingest.status("report.txt")["ready"] is True
    assert ingest.needs("report.txt") == []


def test_a_producer_that_does_not_handle_the_suffix_gets_no_row(tenant_storage):
    counting_producer(handles=(".pdf",))
    a_file(tenant_storage)

    report = ingest.ingest("report.txt")

    assert report["ran"] == []
    assert ingest.status("report.txt")["producers"] == {}
    assert ingest.status("report.txt")["ready"] is True


# --------------------------------------------------------------------------
# invalidation: size, mtime, version
# --------------------------------------------------------------------------

def test_a_bigger_file_is_produced_again(tenant_storage):
    _, calls = counting_producer()
    path = a_file(tenant_storage)
    ingest.ingest("report.txt")

    before = path.stat().st_mtime
    path.write_text("original and then some more", encoding="utf-8")
    # Put the timestamp back, so size is the only thing that changed and this
    # test cannot pass for the wrong reason.
    os.utime(path, (before, before))

    assert ingest.needs("report.txt") == ["fake"]
    ingest.ingest("report.txt")
    assert len(calls) == 2


def test_a_touched_file_of_the_same_size_is_produced_again(tenant_storage):
    _, calls = counting_producer()
    path = a_file(tenant_storage)
    ingest.ingest("report.txt")

    later = path.stat().st_mtime + 10
    os.utime(path, (later, later))

    assert ingest.needs("report.txt") == ["fake"]
    ingest.ingest("report.txt")
    assert len(calls) == 2


def test_a_file_touched_by_half_a_second_is_produced_again(tenant_storage):
    """The mtime comparison is exact, and an earlier version of it was not.

    It carried search.stale()'s one-second tolerance across on the reasoning
    that the two ought to agree about the same file. They serve different
    purposes: stale() produces a suggestion, so being loose costs a rebuild
    nobody needed, while this decides whether derived data may be served for
    bytes that no longer exist.

    Step 2.1 found it -- a PDF rewritten with a body of the same length inside
    the same second, and the index was handed the old text.
    """
    _, calls = counting_producer()
    path = a_file(tenant_storage)
    ingest.ingest("report.txt")

    nudged = path.stat().st_mtime + 0.5
    os.utime(path, (nudged, nudged))

    assert ingest.needs("report.txt") == ["fake"]
    ingest.ingest("report.txt")
    assert len(calls) == 2


def test_a_file_nobody_touched_is_left_alone(tenant_storage):
    """Exact is not the same as paranoid: an unchanged file stays unchanged.

    Including one restored from a backup that preserved its timestamp, which
    is the case a hash would also decline to re-run and a tolerance was never
    needed for.
    """
    _, calls = counting_producer()
    path = a_file(tenant_storage)
    ingest.ingest("report.txt")

    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime))

    assert ingest.needs("report.txt") == []
    ingest.ingest("report.txt")
    assert len(calls) == 1


def test_bumping_the_version_invalidates_what_the_old_one_made(tenant_storage):
    """This is how a fixed bug reaches documents processed before the fix."""
    _, calls = counting_producer(version=1)
    a_file(tenant_storage)
    ingest.ingest("report.txt")

    ingest.unregister("fake")
    _, calls_v2 = counting_producer(version=2)

    assert ingest.needs("report.txt") == ["fake"]
    assert ingest.status("report.txt")["producers"]["fake"]["stale"] is True

    ingest.ingest("report.txt")

    assert calls_v2 == ["report.txt"]
    assert ingest.status("report.txt")["producers"]["fake"]["producer_version"] == 2


# --------------------------------------------------------------------------
# failure is recorded, not raised
# --------------------------------------------------------------------------

def test_a_failing_producer_is_recorded_and_does_not_raise(tenant_storage):
    def explode(source, out_dir):
        raise ValueError("the page images did not come out")

    ingest.register(ingest.Producer(
        name="images", version=1, handles=frozenset({".txt"}), slow=False, run=explode,
    ))
    a_file(tenant_storage)

    report = ingest.ingest("report.txt")

    assert "images" in report["failed"]
    assert "did not come out" in report["failed"]["images"]
    row = ingest.status("report.txt")["producers"]["images"]
    assert row["status"] == ingest.FAILED
    assert "ValueError" in row["detail"]


def test_one_failure_does_not_stop_the_other_producers(tenant_storage):
    """Decision 4.5. An upload does not fail because one producer did."""
    def explode(source, out_dir):
        raise RuntimeError("no")

    ingest.register(ingest.Producer(
        name="aaa_broken", version=1, handles=frozenset({".txt"}), slow=False, run=explode,
    ))
    _, calls = counting_producer(name="zzz_fine")
    a_file(tenant_storage)

    report = ingest.ingest("report.txt")

    assert report["ran"] == ["zzz_fine"], "a producer earlier in the run took the others down"
    assert list(report["failed"]) == ["aaa_broken"]
    assert calls == ["report.txt"]


def test_a_failed_producer_is_retried_and_the_attempts_are_counted(tenant_storage):
    def explode(source, out_dir):
        raise RuntimeError("still no")

    ingest.register(ingest.Producer(
        name="images", version=1, handles=frozenset({".txt"}), slow=False, run=explode,
    ))
    a_file(tenant_storage)

    ingest.ingest("report.txt")
    ingest.ingest("report.txt")
    ingest.ingest("report.txt")

    assert ingest.status("report.txt")["producers"]["images"]["attempts"] == 3
    assert ingest.status("report.txt")["ready"] is False


def test_a_changed_file_starts_the_count_again(tenant_storage):
    """Three failures against the old bytes say nothing about the new ones."""
    def explode(source, out_dir):
        raise RuntimeError("no")

    ingest.register(ingest.Producer(
        name="images", version=1, handles=frozenset({".txt"}), slow=False, run=explode,
    ))
    path = a_file(tenant_storage)
    ingest.ingest("report.txt")
    ingest.ingest("report.txt")
    assert ingest.status("report.txt")["producers"]["images"]["attempts"] == 2

    later = path.stat().st_mtime + 10
    path.write_text("something else entirely", encoding="utf-8")
    os.utime(path, (later, later))
    ingest.ingest("report.txt")

    assert ingest.status("report.txt")["producers"]["images"]["attempts"] == 1


def test_nothing_to_produce_is_not_a_failure(tenant_storage):
    """A PDF with no text in it is `skipped`, and the manifest says which.

    "Nobody has looked at this" and "we looked, and there is nothing here" are
    the two states the search index cannot tell apart today.
    """
    def empty_handed(source, out_dir):
        return ingest.Result.nothing("no text could be extracted from it")

    ingest.register(ingest.Producer(
        name="text", version=1, handles=frozenset({".txt"}), slow=False, run=empty_handed,
    ))
    a_file(tenant_storage)

    report = ingest.ingest("report.txt")

    assert report["failed"] == {}
    assert report["ran"] == ["text"]
    row = ingest.status("report.txt")["producers"]["text"]
    assert row["status"] == ingest.SKIPPED
    assert row["detail"] == "no text could be extracted from it"
    assert ingest.status("report.txt")["ready"] is True
    assert ingest.needs("report.txt") == []


def test_a_producer_that_wrote_nothing_leaves_no_folder_behind(tenant_storage):
    def empty_handed(source, out_dir):
        return ingest.Result.nothing("nothing in it")

    ingest.register(ingest.Producer(
        name="text", version=1, handles=frozenset({".txt"}), slow=False, run=empty_handed,
    ))
    a_file(tenant_storage)
    ingest.ingest("report.txt")

    assert not (config.derived_dir() / "text").exists()


# --------------------------------------------------------------------------
# a missing library is not a broken document
# --------------------------------------------------------------------------

def test_an_environment_fault_is_reported_once_and_recorded_against_nothing(tenant_storage):
    """The lesson app/search.py already paid for.

    Nineteen good PDFs were once indexed as empty and each reported as
    "probably a scan", because the interpreter had no pymupdf. One missing
    package, nineteen wrong conclusions about the documents. Here it must not
    become a `failed` row at all, or installing the package leaves several
    hundred rows that are lies.
    """
    def unavailable(source, out_dir):
        raise ingest.ProducerUnavailable("pymupdf is not installed in this interpreter")

    ingest.register(ingest.Producer(
        name="text", version=1, handles=frozenset({".txt"}), slow=False, run=unavailable,
    ))
    a_file(tenant_storage)
    a_file(tenant_storage, name="second.txt")

    first = ingest.ingest("report.txt")
    second = ingest.ingest("second.txt")

    assert "pymupdf" in first["unavailable"]["text"]
    assert "pymupdf" in second["unavailable"]["text"]
    assert first["failed"] == {}
    # No row at all: the producer reads as never run, not as having failed
    # against this file. Installing the package is then the whole fix.
    assert ingest.status("report.txt")["producers"]["text"]["status"] == "never run"
    assert ingest.status("report.txt")["missing"] == ["text"]
    assert ingest.status("report.txt")["failed"] == []

    summary = ingest.rebuild()
    assert summary["unavailable"] == ["text"]
    assert summary["failed"] == 0


# --------------------------------------------------------------------------
# fast in the request, slow through the job lane
# --------------------------------------------------------------------------

def test_only_fast_defers_the_slow_producers_and_leaves_them_stale(tenant_storage):
    """Decision 4.3. An upload still produces a searchable file immediately.

    Sub-step 2.3 hands the deferred list to the job lane. Until then this is a
    promise the pipeline returns and nobody keeps, which is why the test
    asserts the work is still outstanding rather than merely skipped.
    """
    _, fast_calls = counting_producer(name="text", slow=False)
    _, slow_calls = counting_producer(name="pages", slow=True)
    a_file(tenant_storage)

    report = ingest.ingest("report.txt", only_fast=True)

    assert report["ran"] == ["text"]
    assert report["deferred"] == ["pages"]
    assert fast_calls == ["report.txt"]
    assert slow_calls == []
    assert ingest.needs("report.txt") == ["pages"]
    assert ingest.status("report.txt")["ready"] is False

    ingest.ingest("report.txt")
    assert slow_calls == ["report.txt"]


# --------------------------------------------------------------------------
# a file leaves, and takes everything with it
# --------------------------------------------------------------------------

def test_forget_removes_the_rows_and_the_artifacts(tenant_storage):
    counting_producer(name="text")
    counting_producer(name="pages")
    a_file(tenant_storage)
    ingest.ingest("report.txt")
    assert (config.derived_dir() / "text" / "report.txt").is_dir()

    result = ingest.forget("report.txt")

    assert result["rows"] == 2
    assert sorted(result["removed"]) == ["pages/report.txt", "text/report.txt"]
    assert not (config.derived_dir() / "text" / "report.txt").exists()
    assert not (config.derived_dir() / "pages" / "report.txt").exists()
    assert sorted(ingest.status("report.txt")["missing"]) == ["pages", "text"]


def test_forget_works_after_the_source_file_is_gone(tenant_storage):
    """Which is the only time it is ever called."""
    counting_producer()
    path = a_file(tenant_storage)
    ingest.ingest("report.txt")
    path.unlink()

    result = ingest.forget("report.txt")

    assert result["rows"] == 1
    assert not (config.derived_dir() / "fake" / "report.txt").exists()


def test_forget_takes_the_output_of_a_producer_the_code_no_longer_has(tenant_storage):
    """Deleting a producer from the code does not delete what it made."""
    counting_producer(name="retired")
    a_file(tenant_storage)
    ingest.ingest("report.txt")

    ingest.unregister("retired")
    result = ingest.forget("report.txt")

    assert result["removed"] == ["retired/report.txt"]
    assert not (config.derived_dir() / "retired").exists()


# --------------------------------------------------------------------------
# derived/ is disposable, and that is not an aspiration
# --------------------------------------------------------------------------

def test_the_whole_derived_folder_can_be_deleted_and_rebuilt(tenant_storage):
    import shutil

    counting_producer(name="text")
    a_file(tenant_storage, name="one.txt")
    a_file(tenant_storage, name="two.txt", body="second")
    ingest.rebuild()
    assert ingest.status("one.txt")["ready"] is True

    shutil.rmtree(config.derived_dir())

    assert ingest.status("one.txt")["ready"] is False
    summary = ingest.rebuild()

    assert summary["files_seen"] == 2
    assert summary["ran"] == 2
    assert ingest.status("one.txt")["ready"] is True
    assert ingest.status("two.txt")["ready"] is True


def test_a_file_that_left_takes_its_artifacts_with_it(tenant_storage):
    """2.4. Artifacts are written when a file arrives and never when one leaves.

    `check_search` writes 41 decoy PDFs, deletes them, and used to leave 41
    text artifacts behind on every run.
    """
    counting_producer(name="text")
    one = a_file(tenant_storage, name="one.txt")
    a_file(tenant_storage, name="two.txt", body="second")
    ingest.rebuild()

    one.unlink()
    swept = ingest.forget_missing()

    assert swept["gone"] == ["one.txt"]
    assert swept["rows"] == 1
    assert not (config.derived_dir() / "text" / "one.txt").exists()
    assert (config.derived_dir() / "text" / "two.txt").exists(), "the wrong file's went"


def test_output_with_no_row_behind_it_is_swept_too(tenant_storage):
    """A crash between writing the bytes and recording them leaves this.

    Sweeping only the manifest would leave it on disk with nothing pointing at
    it, which is the harder sort to notice.
    """
    counting_producer(name="text")
    orphan = config.derived_dir() / "text" / "ghost.txt"
    orphan.mkdir(parents=True)
    (orphan / "output.txt").write_text("from a run that never finished", encoding="utf-8")

    swept = ingest.forget_missing()

    assert swept["gone"] == ["ghost.txt"]
    assert swept["rows"] == 0, "there was no row, and that is the point"
    assert not orphan.exists()


def test_a_rebuild_reconciles_before_it_produces(tenant_storage):
    """Otherwise "rebuilt" means a bit less every time it runs."""
    counting_producer(name="text")
    gone = a_file(tenant_storage, name="gone.txt")
    a_file(tenant_storage, name="stays.txt", body="here")
    ingest.rebuild()
    gone.unlink()

    summary = ingest.rebuild()

    assert summary["forgotten"] == ["gone.txt"]
    assert summary["files_seen"] == 1
    assert not (config.derived_dir() / "text" / "gone.txt").exists()
    assert ingest.status("stays.txt")["ready"] is True


def test_sweeping_a_folder_that_lost_nothing_changes_nothing(tenant_storage):
    counting_producer(name="text")
    a_file(tenant_storage)
    ingest.rebuild()

    swept = ingest.forget_missing()

    assert swept == {"gone": [], "rows": 0, "removed": []}
    assert ingest.status("report.txt")["ready"] is True


# --------------------------------------------------------------------------
# the owner rules apply here too
# --------------------------------------------------------------------------

def test_one_tenant_cannot_see_another_tenants_manifest(tenant_storage):
    counting_producer()
    a_file(tenant_storage)
    ingest.ingest("report.txt")

    with context.use_tenant(OTHER_TENANT):
        config.ensure_data_dir()
        with pytest.raises(ingest.IngestError):
            ingest.status("report.txt")
        assert ingest.rebuild()["files_seen"] == 0


def test_ingesting_without_a_tenant_raises(tenant_storage):
    counting_producer()
    a_file(tenant_storage)

    with context.no_tenant():
        with pytest.raises(context.NoTenantError):
            ingest.ingest("report.txt")


def test_a_path_that_tries_to_leave_the_folder_is_refused(tenant_storage):
    counting_producer()
    a_file(tenant_storage)

    with pytest.raises(ingest.IngestError):
        ingest.ingest("../../etc/passwd")
    with pytest.raises(ingest.IngestError):
        ingest.ingest("nothing-here.txt")
