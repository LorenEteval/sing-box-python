#include <string>
#if defined(__MINGW32__) && defined(_M_ARM64)
    // CPython 3.14t uses MSVC's __getReg(18) intrinsic to read the Windows
    // ARM64 thread environment block, but LLVM-MinGW does not provide it.
    // Windows reserves x18 for that pointer, so provide the equivalent here.
    #include <cstdint>
    static inline std::uintptr_t getArm64ThreadPointer()
    {
        std::uintptr_t value;
        __asm__ __volatile__("mov %0, x18" : "=r"(value));
        return value;
    }
    #define __getReg(registerNumber) getArm64ThreadPointer()
#endif
#if defined _WIN64
    #define _hypot hypot
    #include <cmath>
#endif
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#if defined(__MINGW32__) && defined(_M_ARM64)
    #undef __getReg
#endif

#include "singbox.h"

#include <cstdint>
#include <mutex>
#include <stdexcept>
#include <utility>
#include <vector>

namespace py = pybind11;

namespace {

class SingBoxError : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

std::string take_c_string(char* value) {
    if (value == nullptr) {
        return {};
    }
    std::string result(value);
    singbox_free_string(value);
    return result;
}

void throw_if_error(char* error) {
    if (error != nullptr) {
        throw SingBoxError(take_c_string(error));
    }
}

class SingBox {
public:
    SingBox() = default;
    SingBox(const SingBox&) = delete;
    SingBox& operator=(const SingBox&) = delete;

    ~SingBox() noexcept {
        stop_noexcept();
    }

    void startFromJSON(const std::string& config) {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            ensure_idle();
            if (handle_ != 0) {
                throw std::logic_error("this SingBox instance is already running");
            }
            operation_in_progress_ = true;
        }

        std::uint64_t handle = 0;
        char* error = nullptr;
        {
            // gil_scoped_release is RAII: its destructor reacquires the GIL
            // before execution leaves this scope and touches Python again.
            py::gil_scoped_release release;
            error = singbox_instance_start_from_json(
                const_cast<char*>(config.data()),
                config.size(),
                &handle
            );
        }
        {
            std::lock_guard<std::mutex> lock(mutex_);
            operation_in_progress_ = false;
            if (error == nullptr) {
                handle_ = handle;
            }
        }
        throw_if_error(error);
    }

    void stop() {
        std::uint64_t handle = 0;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            ensure_idle();
            if (handle_ == 0) {
                return;
            }
            operation_in_progress_ = true;
            handle = std::exchange(handle_, 0);
        }

        char* error = nullptr;
        {
            py::gil_scoped_release release;
            error = singbox_stop(handle);
        }
        {
            std::lock_guard<std::mutex> lock(mutex_);
            operation_in_progress_ = false;
        }
        throw_if_error(error);
    }

    py::dict queryStats(const std::vector<std::string>& patterns, bool reset, bool regexp) {
        py::object json = py::module_::import("json");
        const std::string encoded_patterns = py::str(
            json.attr("dumps")(patterns, py::arg("ensure_ascii") = false)
        );

        std::uint64_t handle = 0;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            ensure_idle();
            if (handle_ == 0) {
                throw std::logic_error("this SingBox instance is not running");
            }
            operation_in_progress_ = true;
            handle = handle_;
        }

        char* result = nullptr;
        char* error = nullptr;
        {
            py::gil_scoped_release release;
            error = singbox_query_stats(
                handle,
                const_cast<char*>(encoded_patterns.data()),
                encoded_patterns.size(),
                reset ? 1 : 0,
                regexp ? 1 : 0,
                &result
            );
        }
        {
            std::lock_guard<std::mutex> lock(mutex_);
            operation_in_progress_ = false;
        }
        throw_if_error(error);
        const std::string encoded_result = take_c_string(result);
        return json.attr("loads")(encoded_result).cast<py::dict>();
    }

    bool running() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return handle_ != 0;
    }

    std::uint64_t handle() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return handle_;
    }

    SingBox& enter() {
        return *this;
    }

    void exit(const py::object&, const py::object&, const py::object&) {
        stop();
    }

private:
    void ensure_idle() const {
        if (operation_in_progress_) {
            throw std::logic_error("another operation is already in progress on this SingBox instance");
        }
    }

    void stop_noexcept() noexcept {
        std::uint64_t handle = 0;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (handle_ == 0 || operation_in_progress_) {
                return;
            }
            handle = std::exchange(handle_, 0);
        }
        char* error = singbox_stop(handle);
        if (error != nullptr) {
            singbox_free_string(error);
        }
    }

    mutable std::mutex mutex_;
    std::uint64_t handle_ = 0;
    bool operation_in_progress_ = false;
};

void startFromJSON(const std::string& config) {
    // The call intentionally blocks until an OS signal arrives. As above,
    // gil_scoped_release automatically reacquires the GIL if the call returns.
    py::gil_scoped_release release;
    singbox_start_from_json(
        const_cast<char*>(config.data()),
        config.size()
    );
}

std::string native_version() {
    return take_c_string(singbox_version());
}

}  // namespace

PYBIND11_MODULE(_native, module) {
    module.doc() = R"doc(
Native Python bindings for running sing-box from an in-memory JSON configuration.

Use the module-level startFromJSON function in a multiprocessing.Process. Use
SingBox when explicit, non-blocking in-process lifecycle control is required.
)doc";
    auto singbox_error = py::register_exception<SingBoxError>(
        module,
        "SingBoxError",
        PyExc_RuntimeError
    );
    singbox_error.attr("__doc__") =
        "An error reported by the managed sing-box native lifecycle API.";

    py::class_<SingBox>(module, "SingBox", R"doc(
An explicitly managed, non-blocking in-process sing-box instance.

Unlike the module-level startFromJSON function, this class reports startup
errors as Python exceptions and does not wait for operating-system signals.
Call stop explicitly or use the instance as a context manager.
)doc")
        .def(py::init<>(), "Create a stopped sing-box instance.")
        .def(
            "startFromJSON",
            &SingBox::startFromJSON,
            py::arg("json"),
            R"doc(
Start this instance from a UTF-8 JSON configuration string.

The call returns after the service starts. Invalid configuration, construction,
or startup errors raise SingBoxError. Starting an already-running instance
raises RuntimeError.
)doc"
        )
        .def(
            "stop",
            &SingBox::stop,
            "Stop the instance and release its native resources; repeated calls are safe."
        )
        .def(
            "queryStats",
            &SingBox::queryStats,
            py::arg("patterns") = std::vector<std::string>{},
            py::arg("reset") = false,
            py::arg("regexp") = false,
            R"doc(
Return available runtime, Clash, and V2Ray statistics as a dictionary.

patterns filters V2Ray counters. reset clears matched V2Ray counters after
reading, and regexp interprets patterns as regular expressions. Clash and
V2Ray sections are None when their corresponding sing-box services are not
enabled by the configuration. Runtime memory counters are always present.
)doc"
        )
        .def_property_readonly(
            "running",
            &SingBox::running,
            "Whether this instance currently owns a running native service."
        )
        .def_property_readonly(
            "handle",
            &SingBox::handle,
            "Opaque numeric instance identifier, or 0 while stopped."
        )
        .def(
            "__enter__",
            &SingBox::enter,
            py::return_value_policy::reference_internal,
            "Return this instance for use as a context manager."
        )
        .def(
            "__exit__",
            &SingBox::exit,
            "Stop the instance when leaving a context-manager block."
        );

    module.def(
        "startFromJSON",
        &startFromJSON,
        py::arg("json"),
        R"doc(
Start sing-box from JSON and block until SIGINT or SIGTERM is received.

This process-oriented entry point is intended to be the target of a
multiprocessing.Process. Configuration decoding or construction failures exit
the process with status 23. Service startup failures call os.Exit(-1), whose
observed status is platform-dependent. Use SingBox for exception-based,
non-blocking in-process lifecycle management.
)doc"
    );
    module.attr("__version__") = native_version();
}
