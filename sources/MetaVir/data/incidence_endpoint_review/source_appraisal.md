# Incidence endpoint appraisal

The legacy aggressive-prostate review treated some lethal or fatal outcomes as
incident disease when participants were cancer-free at cohort entry. That is
not the endpoint requested here. Cancer-free enrollment does not change the
failure event: an HR for prostate-cancer death is still time to death, and a
lethal composite can be determined by metastasis or death after the initial
diagnosis.

The checkpoint therefore excludes only the reviewed estimate identities listed
in `current_exclusions.json`. Separately reported total prostate-cancer
diagnosis, advanced stage at diagnosis, and high-grade diagnosis estimates from
the same papers remain eligible. The original model responses and review files
remain unchanged as historical evidence; the deterministic projection applies
this later human endpoint decision.
