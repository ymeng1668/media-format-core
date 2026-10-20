#pragma once
#include <string>
#include <vector>
#include <cstdint>

namespace media_format {

// One timed-text cue extracted from media metadata.
// Timing is expressed as a video-frame index — the muxer converts it to the
// output stream's time-base using the same fps the video stream was opened
// with, so subtitles align with the picture even when fps is non-25.
struct TelemetryEvent {
    int64_t     video_frame_idx;       // when this event should appear
    double      duration_s = 3.0;      // how long the cue stays on screen
    std::string text;                  // UTF-8, single line preferred
};

enum class SubtitlePosition {
    Bottom,
    Top
};

struct SubtitleStyle {
    uint8_t r = 0;
    uint8_t g = 255;
    uint8_t b = 0;
    uint8_t a = 255;
    SubtitlePosition position = SubtitlePosition::Bottom;
};

// Mux H264/H265 Annex-B video and decoded PCM audio into any container
// format that libavformat supports (MP4, MKV, MOV, TS, …).
// The output format is auto-detected from the url extension.
// movflags=+faststart is set automatically for MP4/MOV/3GP containers.
// Both temp files are deleted on return regardless of success/failure.
struct UrlWriter {
    // video codec (H264/H265) is detected from the stream via avformat — no is_h265 flag needed.
    // `events` (optional): if non-empty, a third mov_text/subrip stream is
    // added carrying these timed-text cues.
    static int mux(const std::string& video_tmp,
                   const std::string& audio_tmp,   // empty = no audio
                   const std::string& url,
                   uint32_t video_fps,             // 0 = auto-detect from stream
                   uint32_t audio_sample_rate,
                   uint32_t audio_channels,
                   bool     delete_temps = true,   // set false when video_tmp IS the source
                   const std::vector<TelemetryEvent>& events = {},
                   const std::vector<TelemetryEvent>& raw_events = {},
                   const SubtitleStyle& subtitle_style = SubtitleStyle{});
};

} // namespace media_format
