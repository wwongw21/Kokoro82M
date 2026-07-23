# Kokoro Reader Architecture

Status: Accepted — 2026-07-22

## Context

The application originally kept configuration, provider imports, document extraction,
audio encoding, Windows playback, batch conversion, and all Tk widgets in one GUI
module. That made provider startup behavior difficult to reason about and forced
unrelated changes through a single 3,800-line file.

## Decision

Use a lightweight layered package while keeping the existing Tk entry point and
runtime behavior:

- `reader_core.config` owns paths, portable-mode environment setup, catalogs, and
  stable application constants.
- `reader_core.providers` owns lazy imports and the Edge synthesis adapter. Heavy ML
  packages remain unloaded until a provider is used.
- `reader_core.voice_catalog` owns source-qualified Kokoro voice IDs, repository
  routing, language availability, and the official/community collection catalogs.
- `reader_core.chatterbox` owns the isolated Chatterbox worker lifecycle and protocol.
- `reader_core.runtime` owns immutable provider selections, coalesced background
  loading, stale-revision rejection, and serialized runtime replacement.
- `reader_core.profiles` owns bounded Chatterbox sample extraction and safe managed
  profile storage/deletion.
- `reader_core.text`, `audio`, `documents`, and `playback` provide focused,
  UI-independent services.
- `reader_ui.models` owns Tk-facing document state.
- `reader_ui.batch` owns the batch queue UI and workflow as a mixin hosted by the
  main application.
- `kokoro_gui.py` remains the executable composition root and coordinates widgets,
  provider selection, streaming synthesis, and application lifecycle.

Dependencies point inward: UI modules may import core services, while core services
must not import `kokoro_gui` or `reader_ui`. Provider modules do not import each
other. Settings and voice IDs remain compatible with existing `settings.json` files.

## Consequences

- Model-loading and encoding code can be tested without creating a Tk window.
- The Chatterbox process has one owner, making cleanup and protocol errors consistent.
- Tk callbacks never wait for model-cache locks; heavy loads publish results only
  when their captured selection revision is still current.
- Batch UI changes no longer enlarge the application class.
- Adding a provider still requires GUI orchestration code; a future change can define
  a common provider protocol once provider capabilities stabilize.
- The batch mixin relies on a documented host surface (status, validation, synthesis,
  and settings methods) supplied by `KokoroApp`; this avoids a broad rewrite now.

## Verification

Run:

```powershell
.\python\python.exe -m unittest discover -s tests -v
.\python\python.exe -m compileall -q kokoro_gui.py reader_core reader_ui
```

Ruff is run over the entry point and both packages during maintenance releases.
