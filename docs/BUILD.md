# Local build instructions

The trained model and mitmproxy frozen runtime are local release inputs, not Git assets.

```powershell
$env:PYTHONPATH="src"
python -m pytest -q
python scripts/build_release.py --clean
```

The output is `release/Guance/Guance.exe`. The build script copies the locally installed mitmdump runtime and the model file into the onedir package.
