# Third-party notices

This distribution is MIT licensed (see `LICENSE`). It contains material that is
derived from, or was written against, the following third-party work.

## @earendil-works/pi-agent-core 0.86.1 (MIT)

- Source: `pi-agent-core` JavaScript/TypeScript package, version `0.86.1`.
- Frozen commit used for reference: `c7cdb460aa8a0cebef3446c4166729b8a0d97ead`.
- License: MIT, Copyright (c) 2025 Mario Zechner. The full license text is
  reproduced in `LICENSES/pi-agent-core-MIT.txt` and must stay next to this
  notice in source distributions.
- What is derived: the shape of the neutral model loop, the message/transcript
  types, the streaming event vocabulary and the cancellation/queue semantics of
  `yanxu_harness.core`. That material was re-implemented in Python for this
  project; source comments record the correspondence where it matters.
- What is not derived: the tool, skill, decision, storage, provider and context
  layers here are this project's own design and have no upstream counterpart.

The upstream project is not a build, test or runtime dependency of this
distribution. Nothing here fetches, vendors or requires an upstream checkout,
network access or a Node.js toolchain.

## Third-party assets

No third-party fixtures, skills, fonts, images or model files are bundled at
`0.1.0a1`. If a later release bundles any, they must be listed here with their
license and the exact upstream revision before the release is published.
