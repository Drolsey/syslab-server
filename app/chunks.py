"""Step 4.3: cutting a document into passages that can be cited.

An index of whole documents can say "the answer is somewhere in this
fifty-page contract". A retrieval plane has to say WHERE, and it has to say it
in a form the person reading the answer can check against the file themselves.
That is what a chunk is for: a passage, an ordinal, and a pair of offsets into
the extracted text.

WHY THIS IS A MODULE AND NOT A FUNCTION IN app/producers.py
    The same reason app/parse.py is one. `producers.py` is glue -- it knows
    about the pipeline, about directories, about Result. The splitting rule is
    an algorithm with a version, a set of constants somebody will want to argue
    with, and a property that has to hold exactly. Mixing the two means the
    argument about chunk size happens in a file that also knows where derived/
    lives.

    So this module never touches the filesystem and never imports ingest.
    It takes a string and returns passages. `producers.CHUNKS` does the I/O.

DETERMINISM IS THE WHOLE CONTRACT, AND IT IS NOT A NICE-TO-HAVE
    `chunk_id` is `f"{source}#{ordinal}"`. It goes into an index, into an API
    response, and into whatever a customer writes down when they check an
    answer. If the same text splits differently on the next rebuild, every
    citation ever issued now points at a different passage -- and nothing
    anywhere reports that it happened. A wrong chunker is loud. A
    NON-DETERMINISTIC chunker is silent, and that is worse.

    So, deliberately, none of the following appear below: iteration over a set
    or over anything else unordered, `hash()` (salted per interpreter unless
    PYTHONHASHSEED is set), wall-clock time, randomness, locale-dependent case
    or whitespace rules, or any value read from a network.

    THE LAST ONE IS AN AMENDMENT TO THE PLAN, made here rather than quietly.
    Section 5.4 of `docs/plans/step-04-retrieval-plane.md` says tokens are
    "counted with the model's own tokenizer where the box is reachable,
    estimated at 3.5 characters per token where it is not". That cannot stand
    alongside 4.3's own gate. A boundary decided by a tokenizer that is
    sometimes reachable is a boundary that depends on whether the GPU box was
    up when the document was ingested, which is exactly the Tuesday the gate
    was written to forbid. The estimate is used ALWAYS, and it is integer
    arithmetic. A real tokenizer may later inform the CONSTANTS; it may never
    be asked at chunk time.

THE OFFSETS ARE EXACT, AND THAT IS CHECKABLE
    `artifact_text[chunk.start:chunk.end] == chunk.text` for every chunk this
    produces. Not approximately -- the passage returned over the wire is the
    slice, character for character.

    That is why the whitespace trim below MOVES THE OFFSETS instead of
    stripping the string. `text.strip()` would have been one character shorter
    to write and would have made every citation in the system off by however
    many blank lines happened to precede the passage.

    The offsets point into the TEXT ARTIFACT, not into the source file. There
    is no character offset into a PDF, and a page number is not one either.
    `derived/<tenant>/text/<item>/text.txt` is the thing a person can open.

WHAT VERSION 2 IS ALREADY EXPECTED TO BE
    Contextual retrieval: an LLM writing 50-100 tokens of "where this chunk
    sits in the document" and prepending it before indexing. Best-evidenced
    improvement in the field (failure rates 5.7% -> 1.9%) and deferred because
    it needs a model call per chunk. It lands as a `context` field on Chunk and
    a version bump, which re-chunks everything -- which is the machinery Step
    2.4 built and the reason nothing here has to be right for ever.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

# The splitting rule, in the units it was argued in. Section 5.4: 512
# benchmarked best of seven strategies over 50 academic papers (Feb 2026) and
# divides the ~4,000-token retrieval budget into eight passages.
TARGET_TOKENS = 512

# 12.5%, at the BOTTOM of the industry range on purpose. The evidence for
# overlap is weak, the cost is a copy of one eighth of every document, and the
# failure it buys off is real: a sentence cut in half at a boundary is a
# passage that answers nothing and a passage that starts mid-clause. If a
# measurement ever shows it buying nothing, removing it is a version bump.
OVERLAP_TOKENS = 64

# Seven characters to two tokens -- 3.5, the prose figure from section 5.4,
# written as integers so that the arithmetic cannot depend on a float.
#
# It is NOT llm.CHARS_PER_TOKEN, which is 3, and the difference is deliberate
# in both directions. That one estimates a JSON PAYLOAD -- tool definitions,
# quoted strings, database rows -- where punctuation pushes the ratio down, and
# it is pessimistic because under-estimating a prompt costs an HTTP 400 and no
# answer at all. This one estimates PROSE that has already been extracted from
# a document, and being pessimistic here would simply make every chunk 15%
# shorter than the size that was benchmarked.
CHARS_PER_TOKEN_NUMERATOR = 7
CHARS_PER_TOKEN_DENOMINATOR = 2

TARGET_CHARS = TARGET_TOKENS * CHARS_PER_TOKEN_NUMERATOR // CHARS_PER_TOKEN_DENOMINATOR
OVERLAP_CHARS = OVERLAP_TOKENS * CHARS_PER_TOKEN_NUMERATOR // CHARS_PER_TOKEN_DENOMINATOR

# Recursive character splitting: try to break at the most meaningful boundary
# available, and only fall back to a less meaningful one for the pieces that
# are still too long. Paragraph, line, sentence, word.
#
# THERE IS NO EMPTY STRING AT THE END OF THIS TABLE, and the first version of
# it had one, copied from the shape the technique is usually written in. It
# did nothing: _atoms skips a separator it cannot search for and falls
# through to the hard cut below, which is the real floor. A row that reads
# like the safety net while the actual safety net is somewhere else is worse
# than no row, because the next person to change the floor edits this line.
SEPARATORS: tuple[str, ...] = ("\n\n", "\n", ". ", " ")


def tokens_in(text: str) -> int:
    """How many tokens this passage is worth. An ESTIMATE, always, by design.

    See the note on the constants above: a real tokenizer is not asked here,
    because a boundary that depends on whether a server answered is not a
    boundary that survives a rebuild.
    """
    return len(text) * CHARS_PER_TOKEN_DENOMINATOR // CHARS_PER_TOKEN_NUMERATOR


@dataclass(frozen=True)
class Chunk:
    """One passage, and enough to find it again.

    `start` and `end` are character offsets into the text artifact, and
    `text` is exactly `artifact[start:end]`. Nothing here is normalised,
    lower-cased or re-wrapped: the moment the stored text stops being the
    slice, a citation stops being checkable.
    """

    source: str
    ordinal: int
    text: str
    start: int
    end: int
    tokens: int

    @property
    def chunk_id(self) -> str:
        """`source#ordinal`. Stable across a re-chunk at the same version, and
        deliberately NOT across a version bump -- it is not the same passage
        any more, and a citation that survives its own text changing is worse
        than one that breaks."""
        return f"{self.source}#{self.ordinal}"


# --------------------------------------------------------------------------
# the split itself
# --------------------------------------------------------------------------

def _by_separator(text: str, start: int, end: int, separator: str) -> list[tuple[int, int]]:
    """Spans of text[start:end] cut after each separator.

    The separator stays attached to the piece BEFORE it, so the spans are
    contiguous and cover the range exactly. That is what lets a chunk be
    described by the first and last span it contains, with no arithmetic in
    between -- and arithmetic in between is where an off-by-one citation would
    come from.
    """
    spans: list[tuple[int, int]] = []
    at = start
    while at < end:
        found = text.find(separator, at, end)
        if found < 0:
            spans.append((at, end))
            break
        cut = found + len(separator)
        spans.append((at, cut))
        at = cut
    return spans


def _atoms(text: str, start: int, end: int, separators: tuple[str, ...], limit: int) -> list[tuple[int, int]]:
    """Break text[start:end] down until every piece fits, or cannot be broken.

    Recursive in the sense that matters: a piece that is still too long after
    splitting on paragraphs is split again on lines, and only the pieces that
    need it pay for it. A document of ordinary paragraphs never reaches the
    word separator at all.
    """
    if end - start <= limit:
        return [(start, end)]
    for index, separator in enumerate(separators):
        pieces = _by_separator(text, start, end, separator)
        if len(pieces) < 2:
            continue
        out: list[tuple[int, int]] = []
        for piece_start, piece_end in pieces:
            out.extend(_atoms(text, piece_start, piece_end, separators[index + 1:], limit))
        return out
    # THE FLOOR. Nothing in the table separates this, and a 40,000-character
    # run with no whitespace in it is a real thing to find in an extracted
    # document -- a base64 attachment, a minified script, a table dumped
    # without separators. Without this it becomes one chunk that spends the
    # whole retrieval budget on its own, so it is cut at the limit instead.
    return [(at, min(at + limit, end)) for at in range(start, end, limit)]


def _merge(atoms: list[tuple[int, int]], target: int, overlap: int) -> list[tuple[int, int]]:
    """Gather the pieces back up into chunks of about `target`, overlapping.

    The overlap is expressed as "back up over whole pieces until you have
    carried about `overlap` characters", not as "start `overlap` characters
    earlier". Starting at a character count would cut the overlap mid-word,
    which is the failure overlap exists to prevent, reintroduced at the other
    end of the same chunk.
    """
    chunks: list[tuple[int, int]] = []
    total = len(atoms)
    first = 0
    while first < total:
        last = first
        size = 0
        while last < total:
            width = atoms[last][1] - atoms[last][0]
            # `size and` is what stops a chunk being zero pieces wide. With
            # the atoms _atoms actually produces it never fires -- the hard cut
            # there means none of them is larger than `target` -- but a chunker
            # that can emit an empty chunk loops for ever instead of producing
            # a wrong answer, and that is not a way to find out.
            if size and size + width > target:
                break
            size += width
            last += 1
        chunks.append((atoms[first][0], atoms[last - 1][1]))
        if last >= total:
            break
        back = last
        carried = 0
        # `back > first + 1` is the termination guard, and it is the whole
        # reason this loop cannot hang: the next chunk always starts at least
        # one piece further on than this one did.
        while back > first + 1 and carried + (atoms[back - 1][1] - atoms[back - 1][0]) <= overlap:
            back -= 1
            carried += atoms[back][1] - atoms[back][0]
        first = back
    return chunks


def split(text: str, source: str, *, target_chars: int = TARGET_CHARS,
          overlap_chars: int = OVERLAP_CHARS) -> list[Chunk]:
    """Cut one document's extracted text into passages.

    Returns [] for text that is empty or nothing but whitespace. That is not a
    failure and the producer records it as a skip: a scan has no text layer,
    and a document with no text has no passages, which is a different thing
    from a document nobody has looked at.
    """
    if not text.strip():
        return []

    atoms = _atoms(text, 0, len(text), SEPARATORS, target_chars)
    spans = _merge(atoms, target_chars, overlap_chars)

    trimmed: list[tuple[int, int]] = []
    for start, end in spans:
        # Move the offsets, never the string. See the note at the top: a chunk
        # whose text is not exactly artifact[start:end] is a citation that
        # cannot be checked, and strip() is how that happens by accident.
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if start < end:
            trimmed.append((start, end))

    # Numbered AFTER the whitespace-only spans are dropped, so the ordinals of
    # a document run 0..n-1 with no holes. A hole in the ordinals is a chunk_id
    # that resolves to nothing, arriving in an API response as a passage that
    # cannot be fetched again.
    return [
        Chunk(
            source=source,
            ordinal=ordinal,
            text=text[start:end],
            start=start,
            end=end,
            tokens=tokens_in(text[start:end]),
        )
        for ordinal, (start, end) in enumerate(trimmed)
    ]


# --------------------------------------------------------------------------
# the artifact
# --------------------------------------------------------------------------

# Bumped when the SHAPE of chunks.json changes, which is not the same as the
# producer version being bumped when the SPLIT changes. A reader that can cope
# with both shapes needs to know which it is holding, and "the producer was on
# version 3" does not tell it.
FORMAT = 1


def dumps(source: str, text_chars: int, chunks: list[Chunk]) -> str:
    """chunks.json, as a string, byte-for-byte the same for the same input.

    `text_chars` is the length of the artifact these offsets point into, and it
    is recorded so that a mismatch can be NOTICED. Offsets into a text artifact
    that has since been re-extracted are the one way this design goes silently
    wrong, and a length is the cheapest thing that catches it.

    Written with ensure_ascii=False and a fixed two-space indent. Both are
    chosen rather than defaulted: the first because a contract in Arabic should
    be readable in the artifact, the second because the default separators
    leave trailing spaces on every line, which is a diff nobody wants to read.
    """
    payload = {
        "format": FORMAT,
        "source": source,
        "text_chars": text_chars,
        "target_tokens": TARGET_TOKENS,
        "overlap_tokens": OVERLAP_TOKENS,
        "count": len(chunks),
        "chunks": [
            {
                "ordinal": chunk.ordinal,
                "chunk_id": chunk.chunk_id,
                "start": chunk.start,
                "end": chunk.end,
                "tokens": chunk.tokens,
                "text": chunk.text,
            }
            for chunk in chunks
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def loads(payload: str) -> list[Chunk]:
    """The chunks back out of an artifact. Raises ValueError on anything else.

    Deliberately strict about the format number. A file written by a later
    shape read by an earlier reader is how a passage comes back with the right
    text and the wrong offsets, and refusing is recoverable -- everything under
    derived/ rebuilds.
    """
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"this is not a chunks artifact: {exc}") from exc
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ValueError(
            f"chunks artifact format {data.get('format') if isinstance(data, dict) else '?'}, "
            f"and this reader knows {FORMAT}. Delete derived/ and rebuild."
        )
    source = str(data.get("source", ""))
    return [
        Chunk(
            source=source,
            ordinal=int(row["ordinal"]),
            text=str(row["text"]),
            start=int(row["start"]),
            end=int(row["end"]),
            tokens=int(row["tokens"]),
        )
        for row in data.get("chunks", [])
    ]
