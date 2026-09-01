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
if(NOT SINGBOX_CGO_LINK_ITEMS_FILE)
    message(FATAL_ERROR "SINGBOX_CGO_LINK_ITEMS_FILE must name the generated cgo linker item file")
endif()
if(NOT EXISTS "${SINGBOX_CGO_LINK_ITEMS_FILE}")
    message(FATAL_ERROR "Generated cgo linker item file does not exist: ${SINGBOX_CGO_LINK_ITEMS_FILE}")
endif()

include("${SINGBOX_CGO_LINK_ITEMS_FILE}")

if(NOT DEFINED SINGBOX_CGO_LINK_ITEM_COUNT OR NOT SINGBOX_CGO_LINK_ITEM_COUNT MATCHES "^[0-9]+$")
    message(FATAL_ERROR "Generated cgo linker item count is invalid")
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

    if(APPLE AND SINGBOX_CRONET_ARCHIVE)
        # Chromium's archive includes a private libc++/libc++abi. Keep it in a
        # separate image so its exception runtime cannot interpose pybind11's.
        add_library(cronet SHARED src/cronet.c)
        set_target_properties(cronet PROPERTIES OUTPUT_NAME cronet)
        target_link_options(
            cronet PRIVATE
            "-Wl,-force_load,${SINGBOX_CRONET_ARCHIVE}"
        )
        target_link_libraries(
            cronet PRIVATE
            "-lbsm"
            "-lpmenergy"
            "-lpmsample"
            "-lresolv"
            "-framework AppKit"
            "-framework ApplicationServices"
            "-framework CFNetwork"
            "-framework CoreFoundation"
            "-framework CoreGraphics"
            "-framework CoreServices"
            "-framework CoreText"
            "-framework CryptoTokenKit"
            "-framework Foundation"
            "-framework IOKit"
            "-framework LocalAuthentication"
            "-framework Network"
            "-framework OpenDirectory"
            "-framework Security"
            "-framework SystemConfiguration"
            "-framework UniformTypeIdentifiers"
        )
        add_dependencies(_native cronet)
    endif()
endif()

# A Go c-archive does not propagate the native libraries and Apple frameworks
# declared by dependency packages through #cgo LDFLAGS. setup.py discovers
# those items from the active Go package graph and preserves their link order.
if(SINGBOX_CGO_LINK_ITEM_COUNT GREATER 0)
    math(EXPR SINGBOX_CGO_LINK_ITEM_LAST "${SINGBOX_CGO_LINK_ITEM_COUNT} - 1")

    foreach(SINGBOX_CGO_LINK_ITEM_INDEX RANGE 0 ${SINGBOX_CGO_LINK_ITEM_LAST})
        set(SINGBOX_CGO_LINK_ITEM_VARIABLE "SINGBOX_CGO_LINK_ITEM_${SINGBOX_CGO_LINK_ITEM_INDEX}")

        if(NOT DEFINED ${SINGBOX_CGO_LINK_ITEM_VARIABLE})
            message(FATAL_ERROR "Generated cgo linker item ${SINGBOX_CGO_LINK_ITEM_INDEX} is missing")
        endif()

        target_link_libraries(_native PRIVATE "${${SINGBOX_CGO_LINK_ITEM_VARIABLE}}")
    endforeach()
endif()

if(SINGBOX_CRONET_SHARED)
    target_compile_definitions(_native PRIVATE SINGBOX_CRONET_SHARED=1)
endif()
