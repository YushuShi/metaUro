# Aggressive prostate cancer incidence

Availability reviewed on 2026-09-07. The disease identifier is
`aggressive_prostate_cancer`, a child of `prostate_cancer`.

No directly published SEER age-specific, finite-interval probability matching
the study-defined aggressive prostate cancer endpoint was identified in the
reviewed products. Accordingly, the subtype requires a user-supplied cumulative
incidence risk (%) for the selected starting age, follow-up, and population.
Blank aggressive-prostate tables mean unavailable, never zero.

The [SEER Explorer application](https://seer.cancer.gov/statistics-network/explorer/)
lists Prostate without an aggressive subtype. Its risk comparisons use starting
age and interval length; incidence rates and stage distributions are separate
statistics. The [published cancer-site definitions](https://seer.cancer.gov/statistics-network/explorer/cancer-sites.html)
describe malignant cancers and list supported special subtypes, with no
aggressive-prostate definition. This supports the availability decision; it does
not establish that no investigator could derive a custom risk from SEER data.

[SEER Prostate Cancer Stat Facts](https://seer.cancer.gov/statfacts/html/prost.html)
reports general prostate lifetime risk, the age distribution of diagnosed cases,
and stage distribution/survival. Neither a percentage of diagnosed cases in an
age group nor distant-stage incidence is the probability of developing the
broader study-defined aggressive endpoint. MetaVir does not substitute those
quantities, apply a stage fraction to general prostate risk, or use prostate
cancer mortality as the aggressive-diagnosis risk.

The existing published SEER values for general prostate cancer remain usable.
For aggressive disease, `static/seer-baseline.json` records `manual_required` and
the source review; no synthetic incidence values are added. The app can display
ratio evidence with its explicit identity-approximation label while baseline
risk is unavailable. It cannot estimate incident cases or prospective planning
sample size from an unavailable risk. Reported study sample sizes and case counts
remain article data and are not inferred from the general prostate baseline.

If an exact SEER aggressive endpoint becomes available, add only its directly
published values with definition, starting age, follow-up, sex, race/ethnicity,
source identifier, and source key. Recheck definition compatibility before
clearing the manual requirement.
