"""The source seam, Step 4.2.

`docs/plans/step-04-retrieval-plane.md` section 6. The seam's whole claim is
that where material comes from is now ONE decision rather than three sentences
about local files scattered through the pipeline -- so most of what is worth
asserting here is about the pipeline going through it, not about the folder
walk itself, which is the same code it always was.

Every check here was written by breaking the property first. The two that
caught something are marked where they sit.
"""

from __future__ import annotations

import pytest

from app import config, ingest, sources


@pytest.fixture(autouse=True)
def a_registry_no_test_inherits():
    """The registry is module state, for the reason the producer one is.

    A source registered by one test is in the pipeline for every test after
    it, and because `active()` REFUSES when two are registered, the symptom is
    every later ingest raising about routing rather than doing its job.
    """
    saved = sources.registered()
    sources._SOURCES.clear()
    sources._SOURCES.update(saved)
    yield
    sources._SOURCES.clear()
    sources._SOURCES.update(saved)


# --------------------------------------------------------------------------
# what `files` holds
# --------------------------------------------------------------------------

def test_it_lists_what_is_there_with_the_suffix_already_decided(tenant_storage):
    (tenant_storage / "b.PDF").write_text("two", encoding="utf-8")
    (tenant_storage / "a.txt").write_text("one", encoding="utf-8")

    listed = sources.Files().list()

    assert [item.id for item in listed] == ["a.txt", "b.PDF"]
    # Lowered, because producers.handles is a frozenset of lowercase suffixes
    # and an uppercase extension is an ordinary thing for a real upload to
    # have. Returning ".PDF" here would silently drop the file from rebuild().
    assert [item.suffix for item in listed] == [".txt", ".pdf"]


def test_a_folder_is_not_an_item(tenant_storage):
    (tenant_storage / "notes.txt").write_text("x", encoding="utf-8")
    (tenant_storage / "subfolder.txt").mkdir()

    assert [item.id for item in sources.Files().list()] == ["notes.txt"]


def test_it_lists_unfiltered_including_what_no_producer_wants(tenant_storage):
    """The property `ingest.rebuild` relies on the source NOT having.

    A source that returned only handled suffixes would make an unhandled file
    look deleted to forget_missing(), which sweeps on that answer. Filtering
    belongs at the caller, and this is the assertion that keeps it there.
    """
    (tenant_storage / "holiday.jpeg").write_bytes(b"not a document")

    assert [item.id for item in sources.Files().list()] == ["holiday.jpeg"]


def test_fetch_gives_back_a_path_that_can_be_stat_ed(tenant_storage):
    (tenant_storage / "report.txt").write_text("body", encoding="utf-8")

    path = sources.Files().fetch("report.txt")

    assert path.read_text(encoding="utf-8") == "body"
    assert path.stat().st_size == 4


def test_fetching_what_is_not_there_says_so(tenant_storage):
    with pytest.raises(sources.SourceError) as caught:
        sources.Files().fetch("absent.txt")
    assert "absent.txt" in str(caught.value)


def test_a_source_cannot_be_used_to_climb_out_of_the_tenant_folder(tenant_storage):
    """Not a second traversal defence -- the SAME one.

    `Files.fetch` goes through config.resolve_in_data_dir rather than joining
    the name itself, so this asserts the seam did not quietly open a door
    beside the door scripts/check_isolation.py already guards.
    """
    (tenant_storage.parent / "other.txt").write_text("another tenant's", encoding="utf-8")

    for escape in ("../other.txt", "..\\other.txt", "/etc/passwd"):
        with pytest.raises(sources.SourceError):
            sources.Files().fetch(escape)


# --------------------------------------------------------------------------
# the registry, and the refusal that is the point of it
# --------------------------------------------------------------------------

def test_files_is_registered_at_import():
    assert "files" in sources.registered()
    assert sources.active().name == "files"


def test_registered_hands_back_a_copy():
    held = sources.registered()
    held.clear()
    assert "files" in sources.registered()


def test_a_second_source_is_refused_rather_than_guessed_between():
    """The most important assertion in this file.

    The manifest keys on (source_name, producer) with no column for which
    source that name came from, so two sources each holding a contract.pdf
    would share one row and one folder of derived bytes -- corruption that
    surfaces months later as a document whose text belongs to a different
    document. `active()` picking the first would be the bug; refusing is the
    feature, and this is what stops someone removing the refusal as an
    irritation without reading why it is there.
    """
    sources.register(sources.Files())  # already there: still one
    assert sources.active().name == "files"

    class Pretend:
        name = "gdrive"

        def list(self):
            return []

        def fetch(self, item_id):
            raise SourceError  # noqa: F821 - never reached

    sources.register(Pretend())
    with pytest.raises(sources.SourceError) as caught:
        sources.active()

    message = str(caught.value)
    assert "files" in message and "gdrive" in message
    assert "source_name" in message, "the refusal has to say what must be built first"


def test_no_source_at_all_is_a_refusal_and_not_an_empty_folder(tenant_storage):
    """An empty registry must not read as a tenant with nothing in it.

    Silence here would make `rebuild()` report files_seen=0 and
    `forget_missing()` sweep EVERY artifact this tenant has, because nothing
    in the manifest would appear in an empty `list()`.
    """
    (tenant_storage / "real.txt").write_text("still here", encoding="utf-8")
    sources._SOURCES.clear()

    with pytest.raises(sources.SourceError):
        sources.active()
    with pytest.raises(ingest.IngestError):
        ingest.forget_missing()


# --------------------------------------------------------------------------
# the pipeline goes through the seam
# --------------------------------------------------------------------------

def a_counting_source(tenant_storage, *, name="files"):
    """The real folder behind a source that records what was asked of it."""
    calls = []
    real = sources.Files()

    class Recording:
        def __init__(self):
            self.name = name

        def list(self):
            calls.append("list")
            return real.list()

        def fetch(self, item_id):
            calls.append(f"fetch:{item_id}")
            return real.fetch(item_id)

    sources._SOURCES.clear()
    sources.register(Recording())
    return calls


def test_ingest_reaches_its_bytes_through_the_source(tenant_storage, monkeypatch):
    """The seam is load-bearing, not decoration.

    Written by breaking it first: with `ingest._source` still calling
    resolve_in_data_dir directly, this passes `fetch` never being recorded --
    which is exactly how a seam comes to exist in a diagram and nowhere else.
    """
    saved = ingest.registered()
    ingest._PRODUCERS.clear()

    def run(source, out_dir):
        made = out_dir / "out.txt"
        made.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        return ingest.Result.made(made.relative_to(config.derived_dir()))

    ingest.register(ingest.Producer(
        name="fake", version=1, handles=frozenset({".txt"}), slow=False, run=run,
    ))
    try:
        (tenant_storage / "one.txt").write_text("body", encoding="utf-8")
        calls = a_counting_source(tenant_storage)

        ingest.ingest("one.txt")
        assert "fetch:one.txt" in calls

        calls.clear()
        summary = ingest.rebuild()
        assert "list" in calls and summary["files_seen"] == 1
    finally:
        ingest._PRODUCERS.clear()
        ingest._PRODUCERS.update(saved)


def test_a_source_that_cannot_find_it_is_still_an_IngestError(tenant_storage):
    """Callers did not learn a new exception type the day sources arrived.

    `app/intake.py` and `app/main.py` both catch ingest.IngestError and turn it
    into a 404; a SourceError escaping raw would have been a 500 for a file
    that is merely absent.
    """
    with pytest.raises(ingest.IngestError) as caught:
        ingest.needs("nothing-here.txt")
    assert "nothing-here.txt" in str(caught.value)


def test_rebuild_filters_by_suffix_but_the_sweep_does_not(tenant_storage):
    """Two callers of one `list()`, wanting different subsets of it.

    rebuild() must skip a file no producer handles. forget_missing() must NOT
    treat that same file as gone. Breaking this -- filtering inside
    Files.list() -- leaves the .jpeg's artifacts swept away on every rebuild
    while the file sits in the folder, which is the failure this seam was most
    able to introduce.
    """
    saved = ingest.registered()
    ingest._PRODUCERS.clear()

    def run(source, out_dir):
        made = out_dir / "out.txt"
        made.write_text("x", encoding="utf-8")
        return ingest.Result.made(made.relative_to(config.derived_dir()))

    ingest.register(ingest.Producer(
        name="fake", version=1, handles=frozenset({".txt"}), slow=False, run=run,
    ))
    try:
        (tenant_storage / "doc.txt").write_text("text", encoding="utf-8")
        (tenant_storage / "holiday.jpeg").write_bytes(b"pixels")

        summary = ingest.rebuild()
        assert summary["files_seen"] == 1, "the .jpeg has no producer and is not work"

        # The .jpeg is present, so nothing about it is swept -- and this is the
        # half that a filtering source would get wrong.
        assert ingest.forget_missing()["gone"] == []

        (tenant_storage / "holiday.jpeg").unlink()
        assert ingest.forget_missing()["gone"] == [], "it never had artifacts to lose"
    finally:
        ingest._PRODUCERS.clear()
        ingest._PRODUCERS.update(saved)
