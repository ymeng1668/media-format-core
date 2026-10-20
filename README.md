# media-format-core

Independent C++17 shared library for Streamax media-frame files and validated
MP4 metadata. Both `n9m_engine` and `media-extractor` consume this repository as
a pinned Git submodule. Neither consumer's source or application is required
to build this library.

## Scope

- Media-frame headers and extension parsing, including Type 13/15, GPS and alarms.
- Video/AI timestamp alignment and indexed MP4 metadata validation.
- Face and forward-object privacy rendering with the optional vendor AI SDK.
- ADPCM decoding and H.264/H.265 payload muxing with AAC audio and timed text.

There is no camera protocol, socket listener, authentication, camera control,
download/live transport, AES decryption, or CLI entry point. Consumers unwrap
their own transports and provide media payloads. The library is not a detector:
privacy rendering depends on available metadata and verified timing.

## Build

Requires a C++17 compiler, RapidJSON and FFmpeg development libraries
(`libavformat`, `libavcodec`, `libavutil`, `libswresample`).
Unix/MinGW standalone builds discover FFmpeg using pkg-config.
For MSVC set `FFMPEG_DIR`; static builds may additionally need
`MEDIA_FORMAT_FFMPEG_EXTRA_LIBRARIES` with all transitive codec/system libraries.

```bash
cmake -S . -B build
cmake --build build -j4
ctest --test-dir build --output-on-failure
```

Output: `build/bin/libmedia-format-core.so` (Linux),
`media-format-core.dll` (Windows), or `libmedia-format-core.dylib` (macOS).
Set `MEDIA_FORMAT_RUNTIME_DIR` to override the runtime output directory.
Disable tests with `-DMEDIA_FORMAT_BUILD_TESTS=OFF`.
PIC and shared MSVC CRT are enabled; Windows symbols are exported.

## Integration

```cmake
add_subdirectory(deps/media-format-core)
target_link_libraries(my_app PRIVATE media-format::core)
```

The real target/library name is `media-format-core`. Public headers are under
`include/media-format/`, with APIs in namespace `media_format`.
For example:

```cpp
#include <media-format/media_frame.h>
media_format::MediaFrameHeader header;
// media_format::media_frame_parse_fixed(bytes, size, header);
```

Deploy this shared library beside its consumers and rebuild C++ consumers when
updating the API. A parent with platform-specific FFmpeg configuration may
supply a `media-format-ffmpeg` INTERFACE target containing only third-party
FFmpeg requirements; no parent source dependency is permitted.

External `ffmpeg`/`ffprobe` are required for privacy rendering and MP4 timeline
probing. Normal video/PCM muxing uses FFmpeg libraries directly. ADAS parsing
loads the optional SDK selected by `MEDIA_AI_SDK_DIR`; the archived SDK in
`doc/` is unpacked into the build tree. For deployed ADAS use, provide the SDK
directory explicitly rather than relying on a development build path.
On Linux, the vendor dependency has no SONAME. Installed applications also
need the selected SDK's `lib` (and, if needed, `opencv453/lib`) directory in
`LD_LIBRARY_PATH` before launch; build-tree RPATHs are not deployment paths.

## Provenance And Tests

The shared [documentation directory](doc/) includes the
[SDK snapshot](doc/streamax_ainfo_parse_api/2026-09-22/README.md),
[privacy protection gaps](doc/privacy_protection.md), and
[consumer integration notes](doc/media_format_integration.md), and
[extractor integration history](doc/media_sync.md).
The former `n9m_engine/doc/` and `media-extractor/doc/` trees have been
consolidated here, retaining identical existing documents only once.

Media-only code was extracted from the synchronized media-extractor sources
after commit `fc2a068`; the common muxer originated in n9m_engine `v0.1.0`.
Protocol-specific code was not imported. Source comments and vendor documents
retain their original provenance. The vendor ADPCM implementation is retained.
The SDK ZIP SHA-256 is
`d818006b5d817622ac471d9475985701117c0623114176da0604e110595cc0e5`.
The MP4 footer mapping is sample-derived, not a formal vendor specification.

Core tests cover timing/snapshots and dependency boundaries. Consumer suites
cover payload extraction, audio/muxing, privacy output pixels and MP4 timing.
Commit and push shared fixes here first, then update each consumer's submodule
pin. Do not copy implementations back into consumers.
