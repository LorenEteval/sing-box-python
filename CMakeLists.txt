cmake_minimum_required(VERSION 3.15)
project(singbox_native C CXX)

set(CMAKE_C_STANDARD 99)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
set(PYBIND11_FINDPYTHON ON)

find_package(pybind11 CONFIG REQUIRED)

if(NOT SINGBOX_ARCHIVE)
    message(FATAL_ERROR "SINGBOX_ARCHIVE must name the Go c-archive")
endif()
if(NOT SINGBOX_INCLUDE_DIR)
    message(FATAL_ERROR "SINGBOX_INCLUDE_DIR must name the generated-header directory")
endif()

pybind11_add_module(_native src/singbox.cpp)
target_include_directories(_native PRIVATE "${SINGBOX_INCLUDE_DIR}")

if(WIN32)
    if(MINGW)
        target_compile_options(_native PRIVATE "-Wa,-mbig-obj")
        target_link_options(
            _native PRIVATE
            "-static-libgcc" "-static-libstdc++"
            "-Wl,-Bstatic,--whole-archive"
            "-lwinpthread"
            "-Wl,--no-whole-archive"
        )
    endif()
    target_link_libraries(_native PRIVATE "${SINGBOX_ARCHIVE}")
else()
    target_link_libraries(_native PRIVATE "${SINGBOX_ARCHIVE}" "-lresolv")
    if(APPLE)
        target_link_libraries(
            _native PRIVATE
            "-framework CoreFoundation"
            "-framework Security"
            "-framework SystemConfiguration"
        )
    endif()
endif()
