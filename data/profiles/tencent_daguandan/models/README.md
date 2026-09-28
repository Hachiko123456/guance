# Model asset policy

The trained model file `best.npz` is a local release asset and is intentionally excluded from Git history by `.gitignore`. Place it at:

```text
data/profiles/tencent_daguandan/models/best.npz
```

The current development machine has a copy at the user-provided path. Release builds must verify its SHA-256 and include it only in the locally produced distributable when the user explicitly chooses to ship the weights.
