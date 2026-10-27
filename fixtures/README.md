# Fixtures

Fixture manifests (`*.yaml`) describe the audio, the license or consent status (`kind: synthetic | licensed | consented`), the independent reference alignment, and the predefined warm interval. `harness/a2a_harness/fixture.py` loads them.

- **Synthetic contract fixtures** come from `a2a_harness.synth`. They are deterministic for a given seed and contain no speech or voice data. Regenerate them rather than committing WAVs:

  ```
  uv run --project harness python -m a2a_harness.synth --out .local/fixtures/synthetic --sentences 10
  uv run --project harness python -m a2a_harness.synth --out .local/fixtures/synthetic-10min --minutes 10 --id synthetic-zh-10min
  ```

- **Licensed or consented evaluation audio** lives in `.local/fixtures/` (gitignored) or in the restricted research store. Only manifests, fixture IDs, and aggregate metrics go in git. Never commit participant audio, transcripts, or voice prompts.
- Tiny hand-made contract WAVs may go in `fixtures/contract/` (the only WAVs `.gitignore` allows).
