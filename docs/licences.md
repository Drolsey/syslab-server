# Licences

What this project depends on, under what terms, and whether those terms allow a commercial
product to be built on it and hosted for other people.

**How to use this file.** Every row carries either the date its licence was read from a
primary source and the source itself, or the word `unverified`. A primary source is the
project's own repository, documentation, model card or terms page. It is not a blog post, a
search result, or a summary written by someone else, including one written by an assistant.

`unverified` does not mean wrong. Most of the rows below are widely known and almost
certainly correct. It means nobody has looked, so the row is not yet evidence. Verifying one
takes a minute and the result is permanent, so clear them as you touch each component rather
than in one sitting.

**Adding a dependency without adding a row here is incomplete work.**

**Three different things get called "the licence" and they are not the same.** The *software
licence* covers the code that runs a model. The *model licence* covers the weights and can be
stricter than the code that loads them. *Commercial API terms* cover a hosted service, and
are irrelevant here because nothing in the default architecture calls one. Where the code and
the weights differ, both are listed.

---

## Verified against a primary source

Read on the date shown, at the URL shown.

| Component | Licence | Commercial | Source | Read |
|---|---|---|---|---|
| **PyMuPDF** | **AGPL-3.0 or commercial** | **Conditional — see below** | `pymupdf.readthedocs.io/en/latest/about.html` | 2026-09-07 |
| vLLM | Apache-2.0 | Yes | `github.com/vllm-project/vllm` | 2026-09-07 |
| Qwen3-Embedding-0.6B | Apache-2.0 | Yes | `huggingface.co/Qwen/Qwen3-Embedding-0.6B` | 2026-09-07 |
| Gemma 4 | Apache-2.0 | Yes | `ai.google.dev/gemma/terms` | 2026-09-07 |
| Gemma 1–3 | Gemma Terms of Use, not open source | Yes, with conditions | `ai.google.dev/gemma/terms` | 2026-09-07 |
| Piper, active fork | GPL-3.0 | Yes, copyleft | `github.com/OHF-Voice/piper1-gpl` | 2026-09-07 |
| Tailscale, free plan | Client BSD-3; plan terms are non-commercial | **No, on the free plan** | `tailscale.com/pricing` | 2026-09-07 |

---

## In use today, unverified

Pinned in `requirements.txt`, plus the runtime. The licence shown is the commonly understood
one and has not been read from source for this project.

| Component | Version | Licence believed | Status |
|---|---|---|---|
| Ollama | 0.33.3 | MIT | unverified |
| FastAPI | 0.115.4 | MIT | unverified |
| uvicorn | 0.32.0 | BSD-3-Clause | unverified |
| python-multipart | 0.0.12 | Apache-2.0 | unverified |
| python-dotenv | 1.0.1 | BSD-3-Clause | unverified |
| openpyxl | 3.1.5 | MIT | unverified |
| reportlab | 4.2.5 | BSD-3-Clause | unverified |
| psycopg (binary) | 3.2.3 | LGPL-3.0 | unverified, see note |
| pytest | 8.3.3 | MIT | unverified |
| httpx | 0.27.2 | BSD-3-Clause | unverified |
| Qwen3 chat weights | `qwen3:8b` | Apache-2.0 | unverified for this exact tag |
| SQLite and FTS5 | stdlib | Public domain | unverified |

**psycopg note.** If LGPL is correct, it means the library may be used by software under a
different licence as long as it stays a separately replaceable component. Importing it, which
is all this project does, is fine. Statically embedding a modified copy into a distributed
binary is where LGPL begins to ask something, and that is not something this project does.

**On the Qwen row.** The family is Apache-2.0 and that is well established, but a family is
not a model. Check the card for the exact tag being served, because a quantised or fine-tuned
republish can carry different terms from the original.

---

## The one that needs a decision: PyMuPDF

**This is the largest licensing exposure in the codebase and it predates the AI work.**

PyMuPDF is dual-licensed: AGPL-3.0, or a commercial licence sold by Artifex. The project's
own documentation says so, and directs anyone who cannot meet the AGPL's terms to buy the
commercial licence instead.

AGPL differs from GPL in one way that matters here. It reaches software offered to users over
a network, not only software distributed as files. A hosted product built on an AGPL
component can therefore be asked to offer its own source. Whether it actually is, in this
architecture, depends on how the pieces are separated and distributed, and that is a question
for a lawyer rather than an engineer.

It is not a hypothetical dependency. `app/search.py` and `app/tools.py` both use it, and it
is the PDF reader for the entire search path.

### What Step 4.2 actually changed here, measured rather than hoped

**The read path is off PyMuPDF. The dependency stays. The exposure is narrowed, not closed.**

4.2 asked whether adopting Docling retires PyMuPDF, and the honest answer is no — but it is
worth being precise about what did move, because the remaining use is a much smaller target
than the one this section was written about.

| | Before 4.2 | Now |
|---|---|---|
| Reads PDFs for the index and search | PyMuPDF | **Docling / pypdfium2** |
| `tools.read_pdf`, page rendering | PyMuPDF | **PyMuPDF** — unchanged |
| `tools.write_pdf` | PyMuPDF + reportlab | **unchanged** |

So *"it is the PDF reader for the entire search path"*, written above, **is no longer true**
and the sentence is kept only so the change is visible. `app/search.py` no longer reads a PDF
at all; it consumes what `app/parse.py` produced. The single remaining import is
`app/tools.py:213`, where PyMuPDF renders page images and extracts per-page text for the
agent's `read_pdf` tool.

**Why that is still not a retirement.** Rendering is the part pypdfium2 does well and the
part Docling's backends do not expose, so option 3 below is now a *much* smaller change than
it was — one module, one tool, gated by `check_tools` — rather than a rewrite of the search
path. It is not done here because 4.2 is a parser step and swapping a renderer on the way
past is how a step stops being reviewable.

**One thing that got worse and is recorded rather than buried:** `pypdfium2` is now installed
*as well*, so the project currently ships two PDF libraries. That is the cost of the
narrowing, and it goes away with option 3.

Three options, none urgent, all cheaper now than after launch:

1. **Keep it and accept the obligation.** Reasonable if the product is open source anyway.
2. **Buy the commercial licence from Artifex.** A real recurring cost, to be priced rather
   than assumed.
3. **Move the read path to `pypdfium2`** (Apache-2.0 / BSD). A different library with
   different text extraction behaviour, so the swap must be gated: `check_search` at 9 of 9
   and `check_tools` at 12 of 12 on the same documents, before and after.
   — **Half of this is done as of 4.2**, and pypdfium2 is already installed. What is left is
   `tools.read_pdf` and page rendering, which is where pypdfium2 is strongest.

**Status: undecided, and the decision got cheaper.** Record it in an ADR when decided. It
blocks selling to a third party, not any step of the build. The one thing that would change
this from "undecided" to "urgent" is a customer contract requiring source disclosure terms
the AGPL would trigger.

---

## Chosen for the build, not yet installed

| Component | Licence believed | Commercial | Status |
|---|---|---|---|
| vLLM | Apache-2.0 | Yes | **verified**, see first table |
| Qwen3-Embedding-0.6B | Apache-2.0 | Yes | **verified**, see first table |
| Qwen3-Reranker-0.6B | Apache-2.0 | Yes | unverified; optional component |
| faster-whisper | MIT | Yes | unverified |
| Kokoro-82M | Apache-2.0 | Yes | unverified — **verify before Step 6**, it is the default |
| Silero VAD | MIT | Yes | unverified |
| sqlite-vec | Apache-2.0 **and** MIT, dual | Yes | unverified — **verify before Step 5**; see the note below |
| Docling (`docling-slim`, `docling-core`, `docling-parse`) | MIT | Yes | **verified 11 September 2026** — the LICENSE file itself, read by Amro. See the note below |
| pypdfium2 | Apache-2.0 / BSD-3-Clause | Yes | unverified — **installed as of 4.2**, it is what `docling-parse` reads PDFs with. Already named above as the PyMuPDF escape route |
| python-docx | MIT | Yes | unverified — installed 4.2, `.docx` only |
| beautifulsoup4 | MIT | Yes | unverified — installed 4.2, `.html`, `.htm` |
| python-pptx | MIT | Yes | **verified 11 September 2026** — its own LICENSE file, MIT, Steve Canny. Installed 4.2 part two, `.pptx` only |
| marko | MIT | Yes | **verified 11 September 2026** — its own LICENSE file, MIT, Frost Ming. Installed 4.2 part two, `.md` only |
| XlsxWriter | BSD-2-Clause | Yes | **verified 11 September 2026** — its own LICENSE.txt. Pulled in by python-pptx; **nothing in this project imports it** |
| CUAD v1 (test corpus) | CC BY 4.0 | Yes, with attribution | unverified — **verify before 4.1**; it is data, not code, and the note below says why that matters |
| cloudflared | Apache-2.0 | Yes | unverified |
| Docker Engine | Apache-2.0 | Yes | unverified |
| NVIDIA Container Toolkit | Apache-2.0 | Yes | unverified; the driver itself is proprietary but freely redistributable |

**Docling, 11 September 2026. VERIFIED, and this is what verified looks like.** The MIT
licence text was read from the project's own LICENSE file and pasted in full, by a person.
That is a primary source under this file's rule, where the fetch-and-summarise reading of
sqlite-vec below was not, and the difference is the whole reason the rule exists. Adopted as
the document parser in Step 4.2. **If it retires PyMuPDF, it also retires this project's
largest licence risk** — see the AGPL entry above — but that has to be demonstrated on the
same documents, not assumed.

**RESOLVED 11 September 2026, by not installing them.** The note below was written expecting
Docling to download models. **It does not, in the configuration adopted in 4.2.** Docling's
`DocumentConverter` pulls the full ML pipeline — scipy, transformers, torch, 85 packages —
and with it the models this note worried about. `app/parse.py` uses Docling's **backends**
directly instead: 35 packages, 211 MB, **no torch and no model downloads at all**. So the
question does not arise yet. **It returns with OCR in Step 10**, and it returns as a `text`
version bump, at which point this note applies again in full and should be read as written.

**One thing the MIT licence does not cover, and it is not a quibble.** Docling *downloads
models* at runtime — layout, table structure, and an OCR engine. **Those carry their own
terms and are not covered by the library's MIT.** The library being clear is necessary and
not sufficient. Before 4.2 ships, the models Docling actually pulls on this box must be
listed and their terms read, the same way this row was. An OCR engine is the one most likely
to surprise: some are GPL.

**CUAD v1, 11 September 2026, and it is DATA rather than a dependency.** Proposed in 4.1 as
the test corpus: 510 real commercial contracts as PDF *and* text, with 13,000+ expert
annotations across 41 clause categories. The Atticus Project states CC BY 4.0, explicitly
for commercial and non-commercial use — **but that reading came through a search result,
which this file excludes exactly as it excluded sqlite-vec's.** So the row says `unverified`
and the same one minute of somebody opening the licence clears it.

Two things to settle at the same time, because data has questions code does not:

- **CC BY 4.0 requires attribution.** If any part of this corpus is committed to the
  repository, the attribution goes with it, in the corpus folder and not only here.
- **The Atticus Project makes no representation about the underlying contracts**, which are
  public filings from EDGAR. That is fine for a test corpus and is worth knowing before
  anything from it is shown to a customer as an example.

**sqlite-vec, 10 September 2026.** Its repository front page states dual Apache-2.0 / MIT.
That reading came through a fetch-and-summarise tool, which **this file's own rule excludes
as a primary source** — "not a blog post, a search result, or a summary written by someone
else, including one written by an assistant". So the row stays `unverified` and the licence
column now records what to expect rather than what has been established. Clearing it is one
minute of a person opening `LICENSE-APACHE` and `LICENSE-MIT` in `github.com/asg017/sqlite-vec`.

Two other things found at the same time, neither a licence question but both worth having
before Step 5 depends on them: it is **pre-v1 and says to expect breaking changes**, and it
is **brute-force rather than ANN**, which is fine at this project's scale and is a fact to
design around rather than discover.

---

## Rejected, and why

Keeping this list is the point of the file. Each of these looks free until the terms are read.

**Coqui XTTS-v2.** Reported to carry the Coqui Public Model Licence, which is
**non-commercial**. The code being open source does not help; the weights are the product.
Rejected. *Unverified, and deliberately so: nothing is gained by confirming the terms of
something already ruled out on a first reading.*

**pyannote speaker diarisation.** Pulled in by WhisperX, which is otherwise attractive. The
weights are gated behind accepting terms on a model hub. A dependency that requires a human
to click an agreement cannot be part of a reproducible server build. Avoided for the first
version.

**NVIDIA Parakeet.** Reported as CC-BY-4.0, so commercial use is permitted but **attribution
is required**. That is a product decision as much as an engineering one, because something
has to carry the credit. faster-whisper asks nothing, which is why it is the default.
Parakeet stays a real option if its accuracy or speed turns out to matter more, at which
point its licence gets verified properly.

**Piper — a licence that changed under the project.** The original `rhasspy/piper` was MIT
and is archived. Development moved to a GPL-3.0 fork under the Open Home Foundation, which is
verified above. GPL-3.0 run as a separate process is usable commercially and carries no
network clause, so this is not a rejection. It is the reason Kokoro is the default: Apache-2.0
asks nothing at all, and the two are close enough in quality that the licence decides.

**Gemma 1 to 3.** Google's custom Gemma Terms of Use, not an open-source licence. They permit
commercial use but require passing the use restrictions down to anyone you distribute to,
which is an obligation Apache-2.0 does not create. **Gemma 4 switched to Apache-2.0** under
terms effective 1 April 2026. So a Gemma model is a safe default only if it is Gemma 4 or
later. Check the specific model, never the family name. This is the clearest example in the
file of why a family name is not a licence.

**Tailscale on the free plan.** Not a software licence problem; the client is BSD-3-Clause.
The free plan's own terms state it is suitable only for non-commercial use, with the cheapest
commercial plan at 8 USD per user per month. Tailscale stays for operator access to this
machine. It is not the production request path, and Cloudflare Tunnel is the free alternative
that is.

---

## The rule this file exists to enforce

**Free to download is not free to use.** A model on a public hub with a one-click download can
still carry a non-commercial licence, an attribution requirement, or terms binding everyone
you sell to. The download button does not tell you which. Gemma is the proof: same name, same
publisher, two different licences depending on the version number.

So: no model, library or service enters this project without a row here and a named licence.
If it cannot be established from a primary source, the answer is not "probably fine". Either
someone looks, or the component is not used.
