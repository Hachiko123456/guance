# Model asset policy

The trained `best.npz` is a local asset excluded from Git. Place it beside this README when building with the maintained specification. The public directory template is:

```text
data/profiles/<profile>/models/best.npz
```

`<profile>` means the existing compatibility directory, not a new directory to create. Internal identifiers have not been renamed.

At runtime, `GUANCE_MODEL_PATH` can select an external model. That variable does not relocate the build input in `Guance.spec`. The loader checks array structure, finite values and feature/vocabulary compatibility; a fingerprint identifies the file but does not establish its provenance or usage rights.

Publish weights only with explicit authorization and verified distribution rights. Never add training data, captures, credentials or certificate material here. See the build and release guides under `docs/`.
