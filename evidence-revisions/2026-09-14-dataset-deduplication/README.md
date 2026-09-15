# Duplicate-article decisions — 14 September 2026

The curator selected one article from each of the four overlapping article pairs identified in the preceding audit. This revision applies six direct exposure/disease decisions and their applicable inherited appearances. The selected articles retain their scientific estimates and existing eligibility restrictions.

| Exposure | Disease scope | Retain PMID | Exclude alternate PMID |
|---|---|---|---|
| Alcohol | Kidney and renal pelvis cancer | 25866035 | 28159493 |
| Lycopene | Prostate cancer | 15514967 | 17392149 |
| Omega-3 fatty acids | Prostate cancer | 39417685 | 38343844 |
| Omega-6 fatty acids | Prostate cancer | 39417685 | 38343844 |
| Selenium | Prostate cancer | 19066370 | 20924966 |
| Vitamin E | Prostate cancer | 19066370 | 20924966 |

The rejected omega-3 and omega-6 evidence is also removed from inherited PUFA/prostate views. In the Alcohol / Kidney and renal pelvis cancer view, the rejected article is suppressed both directly and when inherited from the narrower RCC scope. Other disease and exposure contexts remain unchanged, including the rejected article's separate RCC context and PMID 17392149's Tea evidence.

The six later curator decisions replay after earlier human/model decisions. Earlier review CSVs and their decision receipts remain intact; in particular, the new omega-6 article choice supersedes the prior review's choice for PMID 38343844 without rewriting that earlier receipt. Original model packets and the separate source projects are preserved.

The canonical screening decisions, served evidence, both related-exposure settings and affected summary outputs are rebuilt. The worker continues to reject evidence writes during normal application use.

- [Revision and file hashes](revision.json)
- [Preceding release manifest](prior-release-manifest.json)
- [Urinary application audit](../../sources/metaPee/combined/curator_dedup/application_audit_2026-09-14.json)
- [Prostate application audit](../../sources/MetaVir/combined/curator_dedup/application_audit.json)
- [Earlier overlapping-article audit](../../reports/duplicate_sample_size_effect_2026-09-14/possible_shared_datasets.md)

The [post-update duplicate audit](../../reports/dataset_deduplication_2026-09-14/README.md) finds zero remaining different-PMID pairs with exactly matching N and effect in both related-exposure modes. The same-PMID matches are preserved.

Validation completed before installation:

- All **54 metaUro application tests passed**, including 17 new checks covering scoped removals, unchanged retained values, both related-exposure modes, PUFA inheritance and preserved Tea evidence.
- **35 urinary-source tests** and **10 prostate-source tests passed**. Reimporting the 96 earlier prostate human-review rows in an isolated copy retained all five newer exclusions and produced an identical canonical packet.
- The retained scientific records are unchanged. The update preserves **2,125 unrelated urinary canonical contexts**, **35,540 unrelated prostate canonical contexts**, and **122 aggressive-prostate scope arrays**.
- Original model data, historical ChatGPT caches and prior human-review receipts are unchanged. The active evidence checksum verification passes for **35,100 files** and all revision provenance files.
- The read-only post-update duplicate scan confirms that the eight different-article matches with related exposures enabled, and six without them, are all resolved.

Restart an already-running metaUro process to load the new evidence release in its workers.
