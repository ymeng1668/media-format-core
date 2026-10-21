#pragma once

#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

namespace media_format {

struct PythonProcessorOptions {
    std::string module;
    std::string class_name;
    std::string config_json = "{}";
    // Absolute import roots for the generic runtime and the chosen plugin.
    std::vector<std::string> python_paths;
    // Optional interpreter/virtualenv executable; used only when this library
    // initializes CPython. An interpreter owned by the host is never replaced.
    std::string python_executable;
    size_t max_frame_bytes = 256 * 1024 * 1024;
    size_t max_result_bytes = 4 * 1024 * 1024;
    bool block_on_failure = true;
};

struct PythonVideoFrame {
    uint8_t* data = nullptr;
    size_t size = 0;
    int width = 0;
    int height = 0;
    size_t stride = 0;
    int64_t pts_us = 0;
    uint64_t frame_index = 0;
    // RGB24 CPU memory only. The owner must retain the entire described byte
    // range. Python views keep it alive, including views retained after process().
    // Do not mutate/recycle the storage while another owner still references it.
    std::shared_ptr<void> owner;
};

struct PythonFrameResult {
    bool valid = false;
    bool degraded = false;
    bool blocked = false;
    std::string json;
    std::string error;
};

// Generic, synchronous RGB24 processing with an independently configured Python
// instance per stream. Calls are serialized; inference runs on a Python-owned
// worker, not a native capture/decoder thread. No FFmpeg or Python types are
// exposed in the public API. The host must close all bridges before finalizing
// its interpreter or unloading this library; this class never finalizes CPython.
// Keep the library loaded until all retained Python frame views are released.
// Submit frames in source order; concurrent callers are serialized, not sorted.
class CorePythonBridge {
public:
    CorePythonBridge();
    ~CorePythonBridge();
    CorePythonBridge(const CorePythonBridge&) = delete;
    CorePythonBridge& operator=(const CorePythonBridge&) = delete;

    static bool available() noexcept;
    bool open(const PythonProcessorOptions& options, std::string& error);
    // metadata_json augments the exact frame_index/time/PTS supplied above.
    // False means an operation failed; valid/degraded/blocked describe a Python
    // result. With block_on_failure, a failed call blacks out valid input pixels.
    bool process(const PythonVideoFrame& frame, PythonFrameResult& result,
                 const std::string& metadata_json = "{}");
    bool reset(std::string& error);
    bool close(std::string& error);

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace media_format
