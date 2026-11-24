# Contributing

Thanks for helping! Bug reports, ideas and pull requests are welcome.

## Development setup

Requires Python 3.14 and [uv](https://docs.astral.sh/uv/).

```bash
uv venv --python 3.14
uv pip install -r requirements_test.txt
```

## Checks

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/python -m pytest --cov
```

The tests use an in-memory Etebase account (`tests/fake_etebase.py`) built on the real encryption code, so no server is needed.

### Interop tests against a real Etebase server

Changes to `custom_components/etesync/etebase/` must keep working with real servers and the official client. Start a throwaway server (needs `git` and `uv`; state lives in `.etebase-test-server/`):

```bash
./scripts/run_test_server.sh
```

In a second terminal:

```bash
uv pip install etebase==0.32.0
ETEBASE_TEST_SERVER=http://127.0.0.1:8033 .venv/bin/python -m pytest tests/etebase -m live_server
```

CI runs the same tests on every push.

### Trying it in Home Assistant

Link or copy `custom_components/etesync` into the `custom_components` directory of a development Home Assistant instance, restart it and add the integration, for example pointing it at the test server above.

## Releases

1. Move the `[Unreleased]` entries in `CHANGELOG.md` to a new `## [0.2.0] - YYYY-MM-DD` section and commit.
2. Tag and push: `git tag -a v0.2.0 -m v0.2.0 && git push origin v0.2.0`.

The release workflow then writes the version into `manifest.json`, builds `etesync.zip` (which HACS installs) and publishes a GitHub release, using the changelog section as release notes. Tags with a suffix such as `v0.2.0-beta.1` become pre-releases. To release an existing tag again, run the **Release** workflow manually and enter the tag.
