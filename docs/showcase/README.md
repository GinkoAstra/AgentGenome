# AgentGenome interactive showcase

A dependency-free, static product tour for the public project site. It explains the cleaning Graph, candidate review, revision, accepted artifacts, and architecture boundaries.

## Local preview

From the repository root:

```bash
python3 -m http.server 8000 --directory docs/showcase
```

Open <http://localhost:8000>. No install, build, model, API service, or network access is required for the tour itself.

The page includes:

- Happy-path and revision-loop scenarios.
- Play, pause, step, reset, speed control, and clickable history.
- Selectable Graph nodes and architecture layers.
- Graph, Task contract, and accepted-output code views.
- Keyboard focus states, reduced-motion support, and responsive layouts.

## Honest boundary

The showcase uses scripted local data derived from `examples/agent-workflow/graph-clean.yaml` and `clean-input.json`. It does not connect to the AgentGenome API, start a model, persist a run, or prove business acceptance. The real example and its receipts remain the source of execution evidence.

The state machine is in `simulation.mjs`; DOM rendering is in `app.mjs`. This keeps scenario behavior testable without a browser framework.

## GitHub Pages

The repository includes `.github/workflows/pages.yml`, which publishes only this directory. To enable it:

1. Push the workflow and showcase files to the repository's `main` branch.
2. In GitHub, open **Settings → Pages**.
3. Set **Build and deployment → Source** to **GitHub Actions**.
4. Run the **Deploy interactive showcase** workflow if it has not run automatically.

The expected project-site URL is:

```text
https://ginkoastra.github.io/AgentGenome/
```

The workflow does not publish internal design records, test fixtures, run state, or the rest of `docs/`.

## Verify

```bash
node --check docs/showcase/app.mjs
node --check docs/showcase/simulation.mjs
node --test tests/control_plane/showcase_simulation.test.mjs
python3 -m pytest tests/control_plane/test_showcase.py -q
```

The Python test uses only the standard library. If pytest is unavailable, import `tests/control_plane/test_showcase.py` and call its four test functions directly; do not install dependencies just for the static tour.

For a real documentation check, also serve the directory and request the page and every local asset over HTTP. Do not validate only by opening a screenshot.
