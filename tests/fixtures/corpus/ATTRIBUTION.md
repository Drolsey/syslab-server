# Attribution

The 52 contracts in `contracts/` and the text in `reference_text/` are a subsample of the
**Contract Understanding Atticus Dataset (CUAD) v1**, curated and maintained by
**The Atticus Project, Inc.**

- **Licence:** Creative Commons Attribution 4.0 International (CC BY 4.0)
- **Obtained from:** https://zenodo.org/records/4595826
- **Licence verified:** 11 September 2026, from `CUAD_v1_README.txt` inside the
  distribution itself, which states: *"CUAD is licensed under the Creative Commons
  Attribution 4.0 (CC BY 4.0) license and free to the public for commercial and
  non-commercial use."*
- **Paper:** https://arxiv.org/abs/2103.06268

The underlying contracts are public filings sourced from EDGAR, the U.S. Securities and
Exchange Commission's disclosure system. The Atticus Project states plainly that it makes
no representations or warranties regarding the licence status of those underlying
contracts. They are used here as test fixtures and are not shown to any customer.

**Changes made to the original.** Fifty-two of the 510 contracts were selected; the PDFs
and their text are otherwise unmodified. **The files were renamed** to `contract_NN.pdf`.
That is not tidying: `app/search.py` indexes a document's name alongside its text, so a
fixture named after its parties would let a party-name query match the *filename*, and the
score would measure the naming rather than the retrieval. `manifest.json` maps every
`contract_NN` back to its original CUAD filename and contract type.

Files in `formats/` are **not** from CUAD. They were written for this project to exercise
formats CUAD does not contain, and carry no third-party rights.
