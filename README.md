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
their own transports and provide media payloads. Existing privacy rendering
depends on available metadata and verified timing. An optional generic Python
frame bridge now supports image-based processing, including a separate
face/license-plate privacy plugin. It does not change the metadata renderer.

## Build

Requires a C++17 compiler, RapidJSON and FFmpeg development libraries
(`libavformat`, `libavcodec`, `libavutil`, `libswresample`, `libswscale`),
and Python 3.11+ development headers/libraries for the default Python-enabled build.
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

## Python Frame Processing

`media_format::CorePythonBridge` is declared in
`include/media-format/core_python_bridge.h`. It accepts owned RGB24 CPU memory,
row stride, frame index and microsecond PTS; it returns structured JSON and
updates those pixels in place. It has no native dependency on pybind11,
a model registry, or any particular detector.

`MEDIA_FORMAT_ENABLE_PYTHON_BRIDGE` defaults to `ON`, including when this
project is built by either application. Install Python 3.11+ development
headers/libraries, FFmpeg's libswscale development files, and NumPy in the
selected interpreter. Set `-DMEDIA_FORMAT_ENABLE_PYTHON_BRIDGE=OFF` explicitly
for a build without Python; the API then reports that Python is unavailable.
Existing build directories configured with `OFF` retain that cached value;
reconfigure them once with `-DMEDIA_FORMAT_ENABLE_PYTHON_BRIDGE=ON`.
On Ubuntu, `sudo apt install python3-dev python3-venv libswscale-dev`
provides the native development files. Then configure:

```bash
cmake -S . -B build-python -DCMAKE_BUILD_TYPE=Release \
  -DPython3_EXECUTABLE=/absolute/venv/bin/python
cmake --build build-python -j4
ctest --test-dir build-python --output-on-failure
```

On Windows, select a matching-architecture Python installation and build with
`--config Release`; on macOS, select the desired Python executable explicitly.
Linux is verified locally; Windows/macOS runtime qualification was not performed.

Example after the caller decodes/converts a frame to RGB24:

```cpp
#include <media-format/core_python_bridge.h>
#include <vector>

media_format::PythonProcessorOptions options;
options.module = "privacy_redaction";
options.class_name = "PrivacyRedactionProcessor";
options.python_paths = {"/absolute/checkout/plugin/python",
                        "/absolute/checkout/plugin/python/ai/privacy-redaction"};
options.python_executable = "/absolute/venv/bin/python";
options.config_json = R"({"preset":"balanced","mode":"pixelate"})";

media_format::CorePythonBridge bridge;
std::string error;
if (!bridge.open(options, error)) {
    // Report through the caller's logger; do not publish unprocessed video.
    return;
}
auto storage = std::make_shared<std::vector<uint8_t>>(width * height * 3);
// Decode into storage, or retain an existing decoder allocation via frame.owner.
media_format::PythonVideoFrame frame;
frame.data = storage->data();
frame.size = storage->size();
frame.width = width;
frame.height = height;
frame.stride = width * 3;
frame.pts_us = pts_us;
frame.frame_index = frame_index;
frame.owner = storage;
media_format::PythonFrameResult result;
if (!bridge.process(frame, result) || !result.valid || result.blocked) {
    // Black output can be intentional fail-closed behavior; never bypass it.
    return;
}
// Consume processed RGB pixels and result.json; close before Python shutdown.
bridge.close(error);
```

One processor instance belongs to one source. Submit in source order; there is
no timestamp-sorting queue. Calls are synchronous and serialized, with one
Python-created worker per instance. The bridge borrows pixels without a frame
copy; a different output buffer returned by the plugin is copied back. Aliases
of the original view are not copied. Python-held views retain the native
allocation; callers must not mutate/recycle storage with outstanding owners.

If the application already owns CPython, the bridge attaches without replacing
or finalizing it. That host must initialize its interpreter before starting
native work, close all bridges before finalization, and keep the library loaded
until retained Python views are released. Plugins must release their workers
and resources in `close()`. Import paths/module names are process-wide CPython
state, not tenant isolation. The bridge returns errors to the caller's logger;
Python plugins are trusted code, not a sandbox.

Custom processors implement `process(rgb_array, metadata)` and return a dict
with Boolean `valid`, optional Boolean `degraded`/`blocked`, optional `frame`
(same-size uint8 RGB), and JSON-compatible metadata. Configuration is passed as
constructor keyword arguments. `reset()`/`close()` are optional. By default,
errors black out a valid input frame, preserving padding; invalid/unowned input
is rejected without dereferencing it. Generic callers can explicitly set
`block_on_failure=false`; privacy workflows should retain the default.

Only the generic Python runtime is installed when the bridge is enabled.
The privacy plugin (`plugin/python/ai/privacy-redaction` in the development checkout),
its models/runtime environment and the privacy evidence tool are never installed. Git source
archives exclude those files through `.gitattributes`. Enabling the
generic bridge does not authorize distributing privacy dependencies.

For file processing, `render_python_video()` in `python_video_render.h`
decodes the primary video stream, calls the bridge on each RGB frame, and
encodes H.264 MP4 while copying audio and subtitle packets. It preserves
presentation timestamps and publishes output only after all frames, codec
flushes, and processor teardown succeed. Blocked/invalid frames abort the file.
It requires libx264 and even video dimensions; an explicitly bridge-disabled
build provides an unavailable stub without adding libswscale.

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
