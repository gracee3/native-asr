# Licensing

The original code, tests, and documentation in `native-asr` are licensed under
the repository's [MIT License](../LICENSE). Copyright remains with the named
copyright holder and individual contributors.

That MIT grant applies only to original repository material. It does not change
the licenses of software built into runtime images, model weights downloaded by
host tools, evaluation datasets, or their accompanying metadata.

## Third-party inventory

- Native runtime revisions are pinned in the Dockerfiles. Upstream license and
  notice material remains authoritative for those components.
- Every model artifact records its source revision, digest, and license in
  [`manifests/models.lock`](../manifests/models.lock). Multi-file model
  components are locked separately in
  [`manifests/model-components.lock`](../manifests/model-components.lock).
- Every evaluation asset records the same provenance and license fields in
  [`manifests/datasets.lock`](../manifests/datasets.lock).
- Built images retain upstream notice files where the packaged runtime provides
  them. Operating-system packages in the images remain governed by their own
  package licenses.

Model licenses in the current catalog include MIT, CC BY 4.0, the NVIDIA Open
Model License, and OpenMDW 1.1. Dataset assets are currently CC BY 4.0. Run
`just list-models` and inspect the lockfiles for the exact artifact you intend
to use or redistribute.

Model files are deliberately excluded from the repository and runtime images.
Downloading a model does not imply that its terms are compatible with every
commercial, redistribution, or derivative use. This document is an inventory
of project boundaries, not legal advice.
