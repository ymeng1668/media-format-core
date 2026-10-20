#pragma once
#include "forward_privacy.h"

namespace media_format {
struct IndexedMediaRecord {
    size_t offset = 0;
    size_t size = 0;
    uint32_t index = 0;
};

// Validate the entire metadata tail before returning records or a timeline.
// Consumers may apply their own metadata presentation without re-parsing indices.
bool index_mp4_metadata(const std::string& input,
                        const std::vector<uint8_t>& data, size_t tail_start,
                        std::vector<IndexedMediaRecord>& records,
                        std::vector<VideoFrameTime>& times,
                        int& width, int& height);

// Requires the indexed Streamax export footer; refuses guessed clock offsets.
bool read_indexed_mp4_metadata(const std::string& input,
                              const std::vector<uint8_t>& data,
                              size_t tail_start,
                              std::vector<MetadataEvent>& events,
                              std::vector<VideoFrameTime>& times,
                              ForwardPrivacy* forward);
}
