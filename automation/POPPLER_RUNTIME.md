# Exact PDF reader for monthly source validation

The source validators require `pdfinfo`, `pdftotext`, and `pdfdetach` from
Poppler **26.07.0**. The installer preserves that exact contract.

`install_poppler.sh` downloads the
[official 26.07.0 release](https://poppler.freedesktop.org/releases/), verifies
SHA256 `304832f48f8a47fdca90c6b6d1f684e68f37c10c9a0726f345f4ca9df4ca01e2`
before extraction, and builds just those three utilities against a static
Poppler core. The source remains available at
`https://poppler.freedesktop.org/poppler-26.07.0.tar.xz`.
The checksum is also recorded by the
[Debian source release](https://tracker.debian.org/news/1778026/accepted-poppler-26070-1-source-amd64-all-into-experimental/).

On an Ubuntu 24.04 workflow runner, use this step before evidence retrieval:

```yaml
- name: Install exact PDF reader
  run: bash automation/install_poppler.sh "$RUNNER_TEMP/poppler-26.07.0"
```

Copy `install_poppler.sh` and `verify_poppler.py` together to another source
repository's `automation/` directory. The script needs no application imports,
API credentials, model calls, or evidence files. It appends the installed binary
directory to `GITHUB_PATH`, so later steps use the verified tools. Ubuntu codec
and font dependencies remain distro-managed; the Poppler source and executable
versions are fixed and each built binary's checksum is saved.

The installer owns the Ubuntu build-dependency list. A repeat invocation first
checks the source receipt, all three binary hashes and exact versions, and the
PDF extraction smoke; a valid prefix is reused without apt or network calls.
An invalid or absent prefix triggers the checked source download, dependency
installation, and rebuild. A Python CLI calling the installer must also prepend
`<prefix>/bin` to its own `os.environ['PATH']` after successful installation;
`GITHUB_PATH` only changes later workflow steps.

`verify_poppler.py` tests all three exact versions plus page count, encryption,
JavaScript, URL and attachment inspection, page boundaries, and complete text
extraction from a deterministic, substantive two-page synthetic PDF. It writes
the proof to the install prefix's `verification.json`. These are installation
checks; all existing per-article identity, integrity, and source checks remain
in force.

The isolated `poppler-runtime-check.yml` workflow runs on the validation branch
only and has no monthly-update job or access to model credentials. Its proof
must pass before this installation step is propagated to production workflows.
