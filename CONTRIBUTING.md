# Contributing

Thanks for your interest in SpliceTrail! Contributions are welcome via pull request.

## Development setup

```bash
# Fork the repo on GitHub, then clone your fork:
git clone https://github.com/<your-username>/splicetrail.git
cd splicetrail
python -m venv venv && source venv/bin/activate
pip install -e ".[dev,api,hash,bench]"   # or: make install-dev
pytest tests/                            # or: make test
```

SpliceTrail supports Python 3.9 to 3.13; CI runs the tests on each of them.
Without the `api`, `hash` and `bench` extras, the tests that need them are
skipped. `pdqhash` (in `hash`) builds from source, so it needs a C++ compiler.

## Workflow

`main` is the integration branch and is protected. External contributors don't
push directly - open a pull request from a fork:

1. **Fork** this repository and clone your fork.
2. Create a topic branch: `git checkout -b my-change`.
3. Make your change, with tests.
4. Run the checks locally (these also run in CI on your PR):
   ```bash
   pytest tests/
   ruff check src tests
   black --check src tests
   mypy src/splicetrail
   ```
   `make lint` runs the last three. CI pins ruff, black and mypy (see
   `.github/workflows/ci.yml`); other versions can disagree on unchanged code.
   CI also builds the package and runs `twine check` on it.
5. Push to your fork and **open a pull request** against `main`.
6. CI must pass and the maintainer will review before merge.

## Code style

- **black** for formatting (`black src tests`, or `make format`), line length 88.
- **ruff** for linting (`ruff check src tests`).
- **mypy** must pass on `src/splicetrail`; add type hints where they add clarity.
- Docstrings for public APIs.
- Changes to matching defaults need numbers from the benchmark runner
  (`python -m splicetrail.bench.runner`, see the README's Benchmark section).

## Scope and direction

Before starting a large feature, please open an issue to discuss it, so we
can agree on the approach and whether it fits the project.

Good places to start:
- Test coverage and edge cases
- Documentation and examples
- Benchmark harness and datasets

## License

By contributing, you agree that your contributions are licensed under the
project's Apache 2.0 license.
