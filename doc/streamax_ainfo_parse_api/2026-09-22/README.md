# Streamax AI Parser Delivery Snapshot

Reviewed and archived on 2026-09-22 from the local, user-supplied
`streamax_ainfo_parse_api/` directory at the original `n9m_engine` repository root. This is a
repository snapshot date, not a vendor release date. The necessary vendor
files are preserved here; the original root-level folder was removed after
archive verification. No ZIP/TAR/7z/RAR packages were found inside that
directory; the ZIPs here were created from the extracted delivery.
The source ZIP contains 46 files (approximately 104 KiB). Local `.gitattributes`
disables line-ending conversion for the preserved vendor material and
checksum manifests, so their hashes remain valid across checkouts.

## Archived Material

| File | Contents |
| --- | --- |
| `streamax_ai_reference_source.zip` | Vendor demo `src/`, `inc/`, CMake files, build and batch-launch scripts, documentation, plus standalone and demo-embedded SDK headers/manifests for all three platforms. Original relative paths and file bytes are preserved. |
| `streamax_ai_sdk.zip` | Complete standalone Linux x86-64 and Windows x64/x86 SDK trees, including parser binaries, import libraries, headers, OpenCV dependencies, and supplied licenses. Archive roots are the three platform directories. |
| `AI_Algorithm_Info_Parse_Library_User_Guide_CN.docx` | Original Chinese Word guide from `使用说明/AI 算法信息解析库使用说明.docx`. |
| `AI_Algorithm_Info_Parse_Library_User_Guide_CN.md` | Original Markdown guide from the demo's `doc/AI算法信息解析库使用说明.md`, copied without translation. |
| `VENDOR_BUILD.md` | Original demo `doc/BUILD.md`, including the vendor's dependency/build instructions. |
| `include/` | Readable copies of the standalone SDK's current API and binary protocol headers. The standalone Linux/x64/x86 headers are byte-identical. |
| `SHA256SUMS` | Checksums of archived files and readable reference copies, relative to this directory. |
| `SOURCE_SHA256SUMS` | Checksums of every file in the ZIP, relative to the original root-level `streamax_ainfo_parse_api/` directory. |
| `SDK_SHA256SUMS` | Checksums of every file in `streamax_ai_sdk.zip`, relative to its extraction directory. |
| `SDK_BINARY_SHA256SUMS` | Historical identity of supplied standalone and demo-embedded SDK parser libraries/import libraries. Paths are relative to the original delivery. Both copies are byte-identical; only the standalone copy is retained in the SDK ZIP. |

The source ZIP is a reference archive, not a standalone buildable vendor demo.
Demo-only FFmpeg/Crypto++/JSON dependencies, generated output, demo executables,
sample recordings, and Git history are deliberately excluded. The separate
SDK ZIP retains all files from the complete standalone SDK, including its
OpenCV runtime/development files and licenses. No original root-level folder
is needed by the core or its consumers. The vendor parser implementation behind the DLL/SO
API is not supplied as source.

During configuration, media-format-core extracts the SDK ZIP into its own
build directory's `_deps/streamax_ai_sdk/` and selects the platform directory.
In either consumer this is normally `<build>/media-format/_deps/streamax_ai_sdk/`.
Extraction is repeated when the archive hash changes or the selected platform
directory is missing. `-DMEDIA_AI_SDK_DIR=/external/platform` overrides this
and skips extraction. For runtime relocation use `--ai-sdk-dir`; Linux also needs
the selected SDK's `lib` and `opencv453/lib` on `LD_LIBRARY_PATH`.

## Changes Relative to the Older References

The older `../rmai_info_parse_api.tar.gz` exposes the JSON-output
`RMAIInfo_Parse(handle, memory, const char**)` interface. The existing
`../../AI_Algorithm_Info_Parse_Library_User_Guide_EN 1.docx` also describes
that older API, not the struct API. Neither should be treated as the current
binary protocol specification. That older TAR archive is a historical reference,
not a file included in this consolidated documentation tree.

- The supplied headers expose `RMAIInfo_ParseStruct` and a new
  `rmai_info_parse_struct_protocol.h`: versioned output, timestamped algorithm
  groups, payload/block offsets, element counts, and four-byte-packed result
  structures. Input/call structures are separate from that packed protocol.
- The SDK covers Linux `x86_64_gcc_13_3_0`, Windows `x64_msvc_2026`, and Windows
  `x86_msvc_2026`. The vendor guide specifies Linux glibc >= 2.38 and Windows
  VS2026/v145 plus Windows SDK 10.0.26100.0. These are vendor requirements,
  not a claim that all platforms were tested here.
- The newer demo adds binary-result decoding (`struct_decode.cpp`),
  timestamp-based overlay matching (`overlay_manager.cpp`), object/lane/sign
  drawing (`overlay_drawer.cpp`), and video decode/encode/output examples.
  The older demo archive has only `func.cpp`, `func.h`, and `main.cpp` under
  its `src/` directory.
- Type 13 ADAS blocks explicitly distinguish vehicles, pedestrians, lane
  points, rolling-stop rectangles, and speed-limit-sign rectangles, along
  with alarms, detection areas, and status text. Vehicle/pedestrian entries
  carry rectangles, target flags, and optional distance.
- DSC blocks describe driver-face rectangles/keypoints, head pose, phone
  rectangles, seatbelt contours, vehicle speed, and detection areas. Some
  algorithm entries are reserved; definitions do not guarantee a camera
  records every field. These are not forward-camera face/plate detections.

These are differences in the supplied documentation/API surface; they do
not prove that all corresponding algorithms were newly added to firmware.

## Delivery Caveats

- `scripts/streamax_deps.py` is referenced by the build guide but is absent;
  the supplied `scripts/` directory is empty. Keep this limitation in mind
  before following the vendor build instructions.
- The demo's `inc/rmai_info_parse_api.h` lacks the standalone SDK's symbol
  export/import annotations. Its protocol header adds an alarm-size
  `static_assert`. Both variants are retained in the ZIP; the readable
  `include/` copies are from the standalone SDK.
- `src/main_new.cpp` is supplied but explicitly excluded by the vendor
  CMake source list. It is archived for provenance, not treated as the
  active entry point.
- `--forward-frame` / `--forward-blur` currently consume only the ADAS
  vehicle/pedestrian rectangles. Archiving other SDK fields does not mark
them as implemented in the shared core or either consumer.
- These files remain vendor reference material. This snapshot does not
  grant additional redistribution rights or change their licensing.

## Verification

From this directory:

```bash
sha256sum -c SHA256SUMS
cmake -E tar tf streamax_ai_reference_source.zip
```

Extract each ZIP to a separate temporary directory. Run `sha256sum -c` with
the absolute path to `SOURCE_SHA256SUMS` from the source extraction directory,
and with the absolute path to `SDK_SHA256SUMS` from the SDK extraction directory.
`SDK_BINARY_SHA256SUMS` retains historical original-delivery paths only; use
`SDK_SHA256SUMS` to verify the retained SDK without that original folder.
