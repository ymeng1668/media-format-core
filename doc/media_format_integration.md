# Shared Media Format Integration

## Baseline

The annotated `v0.1.0` tag in `n9m_engine` preserves its pre-integration implementation.
The shared dependency is the independent `media-format-core` Git repository,
pinned under `deps/media-format-core` in both consumers. `media-extractor`
remains a separate application repository, not a subproject of `n9m_engine`.

## Ownership

- `media-format-core`: media-frame decoding, Type 13/15 metadata, GPS fields,
  metadata/video timing, indexed MP4 validation, privacy rendering, ADPCM,
  and FFmpeg muxing with optional timed-text cues.
- `media_extractor`: standalone media-only CLI and JSON/text presentation.
- `n9m_engine`: camera protocol, connections, control, server handling and API.
- `n9m_app`: protocol/file orchestration and existing CLI/subtitle presentation.

The N9M side unwraps/decrypts protocol payloads before consuming media-format
functions. No protocol source, AES implementation, camera credentials, socket
control, or parent source include path belongs in the media-only repository.
`media_format_boundary` checks the media source tree for protocol leakage.
Existing media headers on the N9M side contain compatibility aliases only;
recompile external C++ consumers after this change (binary ABI is not retained).
Linking the CMake `n9m_engine` target propagates `media-format::core` to
consumers. Direct/manual linker invocations that use media APIs must link both
libraries; camera-only engine code need not load the media library at runtime.

## Build

The actual target and library basename is `media-format-core`, with CMake alias
`media-format::core`. It is a shared library, with PIC and Windows DLL exports.
MSVC consumers and libraries must use the shared CRT.
The parent passes only FFmpeg dependencies through `media-format-ffmpeg`.
The optional CLI and tests can also be built standalone without the parent.
Install/deploy both shared libraries alongside the application.
For installed ADAS rendering, pass the selected SDK directory and put its
`lib`/`opencv453/lib` directories on `LD_LIBRARY_PATH` before launching on Linux.
The vendor dependency lacks a SONAME; a build-tree RPATH is not portable.

The former `n9m_engine/doc/` and `media-extractor/doc/` trees are consolidated
in this repository's `doc/`.
Consumers access it through `deps/media-format-core/doc/`. Identical documents
and SDK archives are kept once; vendor reference bytes and checksums are preserved.
Test fixtures, CLI argument handling and the applications' different JSON/subtitle
formats remain separate from shared parser/muxer ownership.

## Updating

1. Change and test the independent `media-format-core` repository.
2. Commit and push that repository.
3. Update both consumers' core submodule checkouts to the tested commit.
4. Build/test each consumer, then commit and push its submodule pointer.

Use `git submodule update --init --recursive` after pulling the parent.
Do not use a branch-tracking update during a release build; use the pinned
commit. A local `MEDIA_FORMAT_SOURCE_DIR` override is for development only.

## Validation

Linux standalone core and both pinned-submodule builds were exercised. Core
tests, extractor CLI/audio/timing/combined-rendering tests and N9M privacy tests
pass. Both `914` MP4 files render through each application with identical
AI/video match counts. The existing N9M listener test times out; this was also
reproduced by compiling the unchanged networking sources directly without the
media library. Windows/macOS builds were not executed in this environment.
