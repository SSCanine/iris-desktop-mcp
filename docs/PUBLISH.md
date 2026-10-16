# Publishing Iris to PyPI

Once you're ready to make `pip install iris-mcp` work, follow these steps.
This is the maintainer guide; users don't need it.

## One-time setup

1. **Create a PyPI account** at https://pypi.org/account/register/.
   The username doesn't have to match GitHub; the package name
   (`iris-mcp`) is what users see.

2. **Enable 2FA** at https://pypi.org/manage/account/2fa-provisioning/.
   PyPI has required 2FA for all uploaders since 2024. Use a TOTP
   authenticator app (Aegis, 1Password, Authy).

3. **Create an API token** at https://pypi.org/manage/account/token/.
   - Scope: "Project: iris-mcp" if the project already exists on PyPI,
     otherwise "Entire account" for the first publish (you'll narrow
     the scope after the first release).
   - Save the `pypi-<long-token>` string somewhere safe; PyPI shows it
     exactly once.

4. **Install the build + upload tools** (one-time):
   ```powershell
   python -m pip install --upgrade build twine
   ```

## Each release

From the repo root:

```powershell
# 1. Make sure the tree is clean and the version in pyproject.toml +
#    iris/_version.py both match what you're about to release.
git status
type pyproject.toml | findstr version
type iris\_version.py

# 2. Wipe old artifacts.
Remove-Item -Recurse -Force dist, build, iris_mcp.egg-info -ErrorAction SilentlyContinue

# 3. Build sdist + wheel.
python -m build

# 4. Sanity-check the artifacts (catches missing README, malformed
#    metadata, etc. before they hit PyPI).
python -m twine check dist/*

# 5. Optional but recommended: upload to TestPyPI first, install from
#    there in a throwaway venv, confirm iris-mcp works end to end.
python -m twine upload --repository testpypi dist/*
# Install + verify in a clean venv:
python -m venv .venv-test
.venv-test\Scripts\activate
pip install -i https://test.pypi.org/simple/ iris-mcp
iris-mcp-doctor
deactivate
Remove-Item -Recurse -Force .venv-test

# 6. Upload for real.
python -m twine upload dist/*
# Username: __token__
# Password: <paste the pypi-... token>
```

After upload, the package is live at https://pypi.org/project/iris-mcp/
and `pip install iris-mcp` works for anyone.

## Tag and GitHub release

PyPI publication is independent of GitHub releases. Both should happen
together:

```powershell
git tag -a v0.1.x -m "v0.1.x: <short summary>"
git push origin v0.1.x

gh release create v0.1.x --title "v0.1.x" --notes-from-tag dist/*
```

Attaching the dist files to the GitHub release means users who can't
reach PyPI can still grab a wheel directly.

## Versioning

Iris follows semantic versioning:

- `0.1.x` -> bug fixes, new tests, doc improvements
- `0.x.0` -> new MCP tools, new resolver behavior, new bench scenarios.
  Breaking config changes (env var rename, apps.yaml schema change)
  also bump the minor.
- `1.0.0` -> when the tool surface is stable enough that we promise not
  to remove or rename tools without a deprecation window.

## Yanking a bad release

If you discover something wrong with a release after publishing, you
**cannot delete it from PyPI**. You can YANK it, which hides it from
`pip install` resolution but keeps the file accessible for anyone who
pinned it explicitly:

```powershell
python -m twine yank iris-mcp==0.1.0 --reason "<short reason>"
```

The right move is usually to fix the bug, bump to the next patch
version, publish again, and yank the broken release after the fix is
live.
