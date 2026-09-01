# Live-model examples

These are successful OpenRouter outputs captured from the final runtime using
the two commands in `make demo-live`:

- `post_termination_access.md`: positive finding with complete grant chains.
- `access_explain.md`: supported negative finding for a resource that exists.

After configuring `OPENROUTER_API_KEY`, rerun both with:

```bash
make demo-live
```

The target prints reports and stores JSON under `runs/`; it does not overwrite
these Markdown files. The deterministic examples under `../offline/` can be
regenerated without an API key.
