#include "core_python_bridge.h"

#include <cstring>
#include <filesystem>
#include <limits>
#include <mutex>
#include <stdexcept>
#include <utility>

#include <rapidjson/document.h>
#include <rapidjson/stringbuffer.h>
#include <rapidjson/writer.h>

#if MEDIA_FORMAT_ENABLE_PYTHON_BRIDGE
#include <Python.h>
#endif

namespace media_format {
namespace {

bool valid_frame(const PythonVideoFrame& frame, size_t limit)
{
    if (!frame.data || !frame.owner || frame.width <= 0 || frame.height <= 0)
        return false;
    if (static_cast<size_t>(frame.width) > std::numeric_limits<size_t>::max() / 3)
        return false;
    const size_t row = static_cast<size_t>(frame.width) * 3;
    if (frame.stride < row || frame.stride > limit || row > limit)
        return false;
    const size_t previous_rows = static_cast<size_t>(frame.height - 1);
    if (previous_rows > (limit - row) / frame.stride)
        return false;
    return previous_rows * frame.stride + row <= frame.size;
}

void fail(const PythonVideoFrame& frame, PythonFrameResult& result,
          const std::string& error, bool block)
{
    result.valid = false;
    result.degraded = true;
    result.error = error;
    result.blocked = block;
    if (block) {
        const size_t row = static_cast<size_t>(frame.width) * 3;
        for (int y = 0; y < frame.height; ++y)
            std::memset(frame.data + static_cast<size_t>(y) * frame.stride, 0, row);
    }
    rapidjson::StringBuffer buffer;
    rapidjson::Writer<rapidjson::StringBuffer> writer(buffer);
    writer.StartObject();
    writer.Key("valid"); writer.Bool(false);
    writer.Key("degraded"); writer.Bool(true);
    writer.Key("blocked"); writer.Bool(block);
    writer.Key("error"); writer.String(error.c_str(), static_cast<rapidjson::SizeType>(error.size()));
    writer.EndObject();
    result.json.assign(buffer.GetString(), buffer.GetSize());
}

#if MEDIA_FORMAT_ENABLE_PYTHON_BRIDGE
class Object {
public:
    explicit Object(PyObject* object = nullptr) : object_(object) {}
    ~Object() { Py_XDECREF(object_); }
    Object(const Object&) = delete;
    Object& operator=(const Object&) = delete;
    PyObject* get() const { return object_; }
    PyObject* release() { return std::exchange(object_, nullptr); }
private:
    PyObject* object_;
};

class Gil {
public:
    Gil() : state_(PyGILState_Ensure()) {}
    ~Gil() { PyGILState_Release(state_); }
    Gil(const Gil&) = delete;
    Gil& operator=(const Gil&) = delete;
private:
    PyGILState_STATE state_;
};

std::unique_lock<std::mutex> lock_without_gil(std::mutex& mutex)
{
    // A Python-owned caller may already hold the GIL. Never block that GIL
    // while another call waits for the processor's Python worker to finish.
    std::unique_lock<std::mutex> lock(mutex, std::defer_lock);
    PyThreadState* state = PyEval_SaveThread();
    try {
        lock.lock();
    } catch (...) {
        PyEval_RestoreThread(state);
        throw;
    }
    PyEval_RestoreThread(state);
    return lock;
}

std::string python_error()
{
    PyObject* type = nullptr;
    PyObject* value = nullptr;
    PyObject* traceback = nullptr;
    PyErr_Fetch(&type, &value, &traceback);
    PyErr_NormalizeException(&type, &value, &traceback);
    Object type_owner(type), value_owner(value), traceback_owner(traceback);
    Object module(PyImport_ImportModule("traceback"));
    Object formatted(module.get() ? PyObject_CallMethod(module.get(), "format_exception", "OOO",
                     type ? type : Py_None, value ? value : Py_None,
                     traceback ? traceback : Py_None) : nullptr);
    Object separator(PyUnicode_FromString(""));
    Object joined(formatted.get() && separator.get() ?
                  PyUnicode_Join(separator.get(), formatted.get()) : nullptr);
    Object fallback(!joined.get() && value ? PyObject_Str(value) : nullptr);
    const char* text = PyUnicode_AsUTF8(joined.get() ? joined.get() :
                                      fallback.get() ? fallback.get() : Py_None);
    std::string error = text ? text : "Python processor failed";
    PyErr_Clear();
    return error;
}

bool initialize(const std::string& executable, std::string& error)
{
    static std::mutex mutex;
    std::lock_guard<std::mutex> lock(mutex);
    if (Py_IsInitialized())
        return true;
    PyConfig config;
    PyConfig_InitPythonConfig(&config);
    config.install_signal_handlers = 0;
    config.parse_argv = 0;
    PyStatus status = PyStatus_Ok();
    if (!executable.empty())
        status = PyConfig_SetBytesString(&config, &config.program_name, executable.c_str());
    if (!PyStatus_Exception(status))
        status = Py_InitializeFromConfig(&config);
    if (PyStatus_Exception(status))
        error = status.err_msg ? status.err_msg : "Cannot initialize CPython";
    PyConfig_Clear(&config);
    if (PyStatus_Exception(status))
        return false;
    PyEval_SaveThread();
    return true;
}

void release_owner(PyObject* capsule)
{
    auto* owner = static_cast<std::shared_ptr<void>*>(PyCapsule_GetPointer(capsule, "media_format.frame_owner"));
    if (owner)
        delete owner;
    else
        PyErr_Clear();
}
#endif

} // namespace

struct CorePythonBridge::Impl {
    std::mutex mutex;
    PythonProcessorOptions options;
#if MEDIA_FORMAT_ENABLE_PYTHON_BRIDGE
    PyObject* host = nullptr;

    bool close_locked(std::string& error)
    {
        PyObject* previous = std::exchange(host, nullptr);
        if (!previous)
            return true;
        Object owner(previous);
        Object result(PyObject_CallMethod(previous, "close", nullptr));
        if (!result.get()) {
            error = python_error();
            return false;
        }
        return true;
    }
#endif
};

CorePythonBridge::CorePythonBridge() : impl_(std::make_unique<Impl>()) {}

CorePythonBridge::~CorePythonBridge()
{
    try {
        std::string ignored;
        close(ignored);
    } catch (...) {
        // Destruction cannot propagate a processor's Python exception.
    }
}

bool CorePythonBridge::available() noexcept
{
    return MEDIA_FORMAT_ENABLE_PYTHON_BRIDGE != 0;
}

bool CorePythonBridge::open(const PythonProcessorOptions& options, std::string& error)
{
    error.clear();
#if MEDIA_FORMAT_ENABLE_PYTHON_BRIDGE
    if (!initialize(options.python_executable, error))
        return false;
    Gil gil;
    auto lock = lock_without_gil(impl_->mutex);
    if (!impl_->close_locked(error))
        return false;
    impl_->options = options;
    if (options.module.empty() || options.class_name.empty() || !options.max_frame_bytes ||
        !options.max_result_bytes || options.config_json.size() > options.max_result_bytes) {
        error = "Module, class, configuration and resource limits must be valid";
        return false;
    }
    try {
        PyObject* search_path = PySys_GetObject("path");
        for (auto it = options.python_paths.rbegin(); it != options.python_paths.rend(); ++it) {
            if (!std::filesystem::path(*it).is_absolute()) {
                error = "Python import roots must be absolute paths";
                return false;
            }
            Object path(PyUnicode_DecodeFSDefault(it->c_str()));
            const int exists = path.get() ? PySequence_Contains(search_path, path.get()) : -1;
            if (exists < 0 || (!exists && PyList_Insert(search_path, 0, path.get()) < 0)) {
                error = python_error();
                return false;
            }
        }
        Object module(PyImport_ImportModule("media_format_frame_runtime"));
        Object type(module.get() ? PyObject_GetAttrString(module.get(), "FrameProcessorHost") : nullptr);
        Object host(type.get() ? PyObject_CallFunction(type.get(), "sss", options.module.c_str(),
                    options.class_name.c_str(), options.config_json.c_str()) : nullptr);
        if (!host.get()) {
            error = python_error();
            return false;
        }
        impl_->host = host.release();
        return true;
    } catch (const std::exception& exception) {
        error = exception.what();
        return false;
    }
#else
    std::lock_guard<std::mutex> lock(impl_->mutex);
    impl_->options = options;
    error = "Python bridge is disabled; configure MEDIA_FORMAT_ENABLE_PYTHON_BRIDGE=ON";
    return false;
#endif
}

bool CorePythonBridge::process(const PythonVideoFrame& input, PythonFrameResult& result,
                               const std::string& metadata_json)
{
    result = {};
    PythonVideoFrame frame = input;
    if (frame.stride == 0 && frame.width > 0)
        frame.stride = static_cast<size_t>(frame.width) * 3;
#if MEDIA_FORMAT_ENABLE_PYTHON_BRIDGE
    if (Py_IsInitialized()) {
        Gil gil;
        auto lock = lock_without_gil(impl_->mutex);
        const bool valid = valid_frame(frame, impl_->options.max_frame_bytes);
        if (!valid || !impl_->host || metadata_json.size() > impl_->options.max_result_bytes) {
            fail(frame, result, valid ? "Processor is not open or frame metadata is too large" :
                 "Invalid RGB24 frame, byte range or allocation owner", valid && impl_->options.block_on_failure);
            return false;
        }
        try {
            auto owner = std::make_unique<std::shared_ptr<void>>(frame.owner);
            Object capsule(PyCapsule_New(owner.get(), "media_format.frame_owner", release_owner));
            if (!capsule.get()) {
                fail(frame, result, python_error(), impl_->options.block_on_failure);
                return false;
            }
            owner.release();
            Object output(PyObject_CallMethod(impl_->host, "process", "KiiKOLKs",
                          static_cast<unsigned long long>(reinterpret_cast<uintptr_t>(frame.data)),
                          frame.width, frame.height, static_cast<unsigned long long>(frame.stride), capsule.get(),
                          static_cast<long long>(frame.pts_us),
                          static_cast<unsigned long long>(frame.frame_index), metadata_json.c_str()));
            if (!output.get()) {
                fail(frame, result, python_error(), impl_->options.block_on_failure);
                return false;
            }
            Py_ssize_t size = 0;
            const char* json = PyUnicode_AsUTF8AndSize(output.get(), &size);
            if (!json || static_cast<size_t>(size) > impl_->options.max_result_bytes) {
                fail(frame, result, json ? "Processor result exceeds the configured limit" : python_error(),
                     impl_->options.block_on_failure);
                return false;
            }
            rapidjson::Document document;
            document.Parse(json, static_cast<size_t>(size));
            if (document.HasParseError() || !document.IsObject() || !document.HasMember("valid") ||
                !document["valid"].IsBool()) {
                fail(frame, result, "Invalid processor result JSON", impl_->options.block_on_failure);
                return false;
            }
            if (!document["valid"].GetBool()) {
                fail(frame, result, document.HasMember("reason") && document["reason"].IsString() ?
                     document["reason"].GetString() : "Processor returned valid=false", impl_->options.block_on_failure);
                return false;
            }
            result.valid = true;
            result.degraded = document.HasMember("degraded") && document["degraded"].IsBool() && document["degraded"].GetBool();
            result.blocked = document.HasMember("blocked") && document["blocked"].IsBool() && document["blocked"].GetBool();
            result.json.assign(json, static_cast<size_t>(size));
            return true;
        } catch (const std::exception& exception) {
            fail(frame, result, exception.what(), impl_->options.block_on_failure);
            return false;
        }
    }
#endif
    std::lock_guard<std::mutex> lock(impl_->mutex);
    fail(frame, result, "Python processor is not available or not open",
         valid_frame(frame, impl_->options.max_frame_bytes) && impl_->options.block_on_failure);
    return false;
}

bool CorePythonBridge::reset(std::string& error)
{
    error.clear();
#if MEDIA_FORMAT_ENABLE_PYTHON_BRIDGE
    if (Py_IsInitialized()) {
        Gil gil;
        auto lock = lock_without_gil(impl_->mutex);
        if (!impl_->host) {
            error = "Python processor is not open";
            return false;
        }
        Object result(PyObject_CallMethod(impl_->host, "reset", nullptr));
        if (!result.get()) {
            error = python_error();
            return false;
        }
        return true;
    }
#endif
    error = "Python processor is not available or not open";
    return false;
}

bool CorePythonBridge::close(std::string& error)
{
    error.clear();
#if MEDIA_FORMAT_ENABLE_PYTHON_BRIDGE
    if (Py_IsInitialized()) {
        Gil gil;
        auto lock = lock_without_gil(impl_->mutex);
        return impl_->close_locked(error);
    }
    std::lock_guard<std::mutex> lock(impl_->mutex);
    // Finalized interpreters cannot accept decref. Closing before host shutdown
    // is part of the API lifetime contract; never call into a dead interpreter.
    impl_->host = nullptr;
#endif
    return true;
}

} // namespace media_format
