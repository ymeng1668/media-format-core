#include "core_python_bridge.h"

#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <thread>
#include <vector>

#if MEDIA_FORMAT_TEST_PYTHON_HOST
#include <Python.h>
#endif

namespace {
void check(bool ok, const std::string& error)
{
    if (!ok) {
        std::fprintf(stderr, "%s\n", error.c_str());
        std::exit(1);
    }
}

media_format::PythonVideoFrame frame()
{
    auto storage = std::make_shared<std::vector<uint8_t>>(48, 165);
    media_format::PythonVideoFrame result;
    result.data = storage->data();
    result.size = storage->size();
    result.width = 4;
    result.height = 3;
    result.stride = 16;
    result.pts_us = -1000;
    result.frame_index = 23;
    result.owner = storage;
    return result;
}

void check_blocked(const media_format::PythonVideoFrame& input)
{
    for (int y = 0; y < input.height; ++y) {
        for (size_t x = 0; x < input.stride; ++x)
            check(input.data[y * input.stride + x] == (x < 12 ? 0 : 165),
                  "Fail-closed pixels or untouched row padding are incorrect");
    }
}
} // namespace

int main(int argc, char** argv)
{
    using namespace media_format;
    std::string error;
    PythonFrameResult result;
    check(CorePythonBridge::available() == static_cast<bool>(MEDIA_FORMAT_TEST_PYTHON_HOST),
          "Loaded library does not match the configured Python bridge mode");
    if (!CorePythonBridge::available()) {
        CorePythonBridge bridge;
        check(!bridge.open({}, error), "Disabled bridge unexpectedly initialized");
        auto input = frame();
        check(!bridge.process(input, result) && result.blocked, "Disabled processing did not fail closed");
        check_blocked(input);
        return 0;
    }
    check(argc >= 4, "Expected runtime directory, processor directory, Python executable");
    PythonProcessorOptions options;
    options.module = "python_bridge_processors";
    options.class_name = "Probe";
    options.python_paths = {argv[1], argv[2]};
    options.python_executable = argv[3];

#if MEDIA_FORMAT_TEST_PYTHON_HOST
    PyThreadState* host_state = nullptr;
    if (argc > 4 && std::string(argv[4]) == "host") {
        PyConfig config;
        PyConfig_InitPythonConfig(&config);
        check(!PyStatus_Exception(PyConfig_SetBytesString(&config, &config.program_name, argv[3])), "Host program path");
        check(!PyStatus_Exception(Py_InitializeFromConfig(&config)), "Host interpreter startup");
        PyConfig_Clear(&config);
        host_state = PyEval_SaveThread();
    }
#endif
    {
        CorePythonBridge bridge;
        options.config_json = "{\"value\":17,\"hold\":true}";
        check(bridge.open(options, error), error);
        auto input = frame();
        check(bridge.process(input, result, "{\"tag\":\"native\",\"frame_index\":999}"), result.error);
        check(result.valid && input.data[0] == 17 && input.data[12] == 165, "Zero-copy write or row stride was lost");
        check(result.json.find("\"pts_us\":-1000") != std::string::npos &&
              result.json.find("\"frame_index\":23") != std::string::npos, "Exact PTS/index metadata was lost");
        check(result.json.find(std::to_string(reinterpret_cast<uintptr_t>(input.data))) != std::string::npos,
              "NumPy did not borrow the original allocation");
        std::weak_ptr<void> retained = input.owner;
        input.owner.reset();
        check(!retained.expired(), "Retained NumPy view lost its native allocation owner");
        check(bridge.reset(error), error);
        check(retained.expired(), "Reset leaked the retained native frame allocation");
        std::atomic<bool> succeeded{true};
        std::vector<std::thread> workers;
        for (int index = 0; index < 4; ++index) {
            workers.emplace_back([&] {
                for (int n = 0; n < 3; ++n) {
                    auto item = frame();
                    PythonFrameResult output;
                    if (!bridge.process(item, output) || item.data[0] != 17)
                        succeeded = false;
                }
            });
        }
        for (auto& worker : workers)
            worker.join();
        check(succeeded, "Concurrent native calls failed or deadlocked");
        check(bridge.close(error) && bridge.close(error), error);
        input = frame();
        check(!bridge.process(input, result) && result.blocked, "Closed bridge did not fail closed");
        check_blocked(input);

        for (const char* name : {"Raises", "Invalid", "WrongShape", "LargeResult"}) {
            options.class_name = name;
            options.config_json = "{}";
            options.max_result_bytes = 256;
            check(bridge.open(options, error), error);
            input = frame();
            check(!bridge.process(input, result) && result.blocked && !result.error.empty(),
                  std::string("Processor failure was hidden: ") + name);
            check_blocked(input);
        }
        options.class_name = "Replacement";
        options.max_result_bytes = 4096;
        check(bridge.open(options, error), error);
        input = frame();
        check(bridge.process(input, result), result.error);
        check(input.data[0] == 37 && input.data[32] == 37 && input.data[12] == 165,
              "Replacement image did not preserve stride/padding");
        input.owner.reset();
        check(!bridge.process(input, result) && !result.blocked, "Unowned native memory was accepted");
        options.module = "missing_media_format_processor";
        check(!bridge.open(options, error) && !error.empty(), "Missing module was silently accepted");
        check(bridge.close(error), error);
    }
#if MEDIA_FORMAT_TEST_PYTHON_HOST
    if (host_state) {
        PyEval_RestoreThread(host_state);
        {
            // Python-owned callers can enter while already holding the GIL.
            CorePythonBridge bridge;
            options.module = "python_bridge_processors";
            options.class_name = "Probe";
            options.config_json = "{}";
            check(bridge.open(options, error), error);
            auto input = frame();
            check(bridge.process(input, result), result.error);
            check(bridge.reset(error), error);
            check(bridge.close(error), error);
        }
        check(Py_FinalizeEx() == 0, "Host interpreter shutdown failed");
    }
#endif
    return 0;
}
