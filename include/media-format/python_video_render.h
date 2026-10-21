#pragma once

#include "core_python_bridge.h"

namespace media_format {

// Decode the primary video stream, process RGB frames through the Python
// bridge, and encode H.264 MP4. Audio and subtitle packets are copied with
// their timestamps. Only a completely processed file is published; a blocked
// or invalid frame aborts the conversion and leaves existing output intact.
// Constructor options include probed source_width/source_height/source_fps;
// processors must accept these hints. Frame indices start at 1.
bool render_python_video(const std::string& input, const std::string& output,
                         const PythonProcessorOptions& options, std::string& error);

} // namespace media_format
