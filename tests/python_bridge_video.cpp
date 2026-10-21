// Focused raw-RGB evidence tool. It is not installed or part of the library API.
#include "core_python_bridge.h"

#include <cstdio>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <limits>
#include <stdexcept>
#include <vector>

int main(int argc, char** argv)
{
    if (argc != 12 && argc != 13) {
        std::fprintf(stderr, "Expected: runtime-root plugin-root python config width height fps-num fps-den input.rgb output.rgb results.jsonl [metadata.json]\n");
        return 1;
    }
    try {
        const int width = std::stoi(argv[5]);
        const int height = std::stoi(argv[6]);
        const int fps_num = std::stoi(argv[7]);
        const int fps_den = std::stoi(argv[8]);
        if (width <= 0 || height <= 0 || width > 16384 || height > 16384 || fps_num <= 0 || fps_den <= 0)
            throw std::runtime_error("Invalid geometry or FPS");
        const size_t size = static_cast<size_t>(width) * height * 3;
        if (size > 256 * 1024 * 1024)
            throw std::runtime_error("Frame exceeds the evidence-tool limit");
        if (std::filesystem::weakly_canonical(argv[9]) == std::filesystem::weakly_canonical(argv[10]))
            throw std::runtime_error("Input and output must differ");
        std::ifstream config(argv[4]);
        if (!config)
            throw std::runtime_error("Cannot read configuration");
        media_format::PythonProcessorOptions options;
        options.module = "privacy_redaction";
        options.class_name = "PrivacyRedactionProcessor";
        options.python_paths = {argv[1], argv[2]};
        options.python_executable = argv[3];
        options.config_json.assign(std::istreambuf_iterator<char>(config), std::istreambuf_iterator<char>());
        std::string metadata_json = "{}";
        if (argc == 13) {
            std::ifstream metadata(argv[12]);
            if (!metadata)
                throw std::runtime_error("Cannot read supplied source metadata");
            metadata_json.assign(std::istreambuf_iterator<char>(metadata), std::istreambuf_iterator<char>());
        }
        media_format::CorePythonBridge bridge;
        std::string error;
        if (!bridge.open(options, error))
            throw std::runtime_error(error);
        std::ifstream source(argv[9], std::ios::binary);
        std::ofstream target(argv[10], std::ios::binary);
        std::ofstream results(argv[11]);
        if (!source || !target || !results)
            throw std::runtime_error("Cannot open input/output evidence files");
        std::shared_ptr<std::vector<uint8_t>> storage;
        uint64_t index = 0;
        for (;;) {
            // Reuse only when Python has released every borrowed view.
            if (!storage || storage.use_count() != 1)
                storage = std::make_shared<std::vector<uint8_t>>(size);
            source.read(reinterpret_cast<char*>(storage->data()), static_cast<std::streamsize>(size));
            if (source.gcount() == 0 && source.eof())
                break;
            if (source.gcount() != static_cast<std::streamsize>(size))
                throw std::runtime_error("Truncated RGB input");
            media_format::PythonVideoFrame frame;
            frame.data = storage->data();
            frame.size = size;
            frame.width = width;
            frame.height = height;
            frame.stride = static_cast<size_t>(width) * 3;
            frame.frame_index = index;
            frame.pts_us = static_cast<int64_t>(index * 1000000ULL * fps_den / fps_num);
            frame.owner = storage;
            media_format::PythonFrameResult result;
            if (!bridge.process(frame, result, metadata_json) || !result.valid || result.blocked)
                throw std::runtime_error(result.error.empty() ? "Privacy backend blocked the frame; inspect models/configuration" : result.error);
            target.write(reinterpret_cast<const char*>(storage->data()), static_cast<std::streamsize>(size));
            results << result.json << '\n';
            if (!target || !results)
                throw std::runtime_error("Cannot write output evidence");
            ++index;
        }
        if (!index)
            throw std::runtime_error("Input contains no RGB frames");
        if (!bridge.close(error))
            throw std::runtime_error(error);
        return 0;
    } catch (const std::exception& error) {
        std::fprintf(stderr, "%s\n", error.what());
        return 1;
    }
}
