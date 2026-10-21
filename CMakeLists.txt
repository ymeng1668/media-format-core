cmake_minimum_required(VERSION 3.16)
project(media_format_core VERSION 0.1.0 LANGUAGES CXX)

if(CMAKE_SOURCE_DIR STREQUAL CMAKE_CURRENT_SOURCE_DIR)
    include(CTest)
endif()
option(MEDIA_FORMAT_BUILD_TESTS "Build shared media-format regression tests" ${BUILD_TESTING})
option(MEDIA_FORMAT_ENABLE_PYTHON_BRIDGE "Enable optional CPython RGB frame processing" OFF)
set(MEDIA_FORMAT_RUNTIME_DIR "${CMAKE_CURRENT_BINARY_DIR}/bin" CACHE PATH "Media shared-library output directory")

find_path(RAPIDJSON_INCLUDE_DIR rapidjson/document.h)
if(NOT RAPIDJSON_INCLUDE_DIR)
    message(FATAL_ERROR "RapidJSON headers not found.")
endif()

add_library(media-format-core SHARED
    src/media_frame.cpp
    src/forward_privacy.cpp
    src/metadata_timing.cpp
    src/privacy_render.cpp
    src/core_python_bridge.cpp
    src/python_video_render.cpp
    src/mp4_metadata.cpp
    src/url_writer.cpp
    third_party/AdpcmCodec.cpp)
add_library(media-format::core ALIAS media-format-core)
target_compile_features(media-format-core PUBLIC cxx_std_17)
set_target_properties(media-format-core PROPERTIES
    POSITION_INDEPENDENT_CODE ON
    WINDOWS_EXPORT_ALL_SYMBOLS ON
    MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>DLL"
    RUNTIME_OUTPUT_DIRECTORY "${MEDIA_FORMAT_RUNTIME_DIR}"
    LIBRARY_OUTPUT_DIRECTORY "${MEDIA_FORMAT_RUNTIME_DIR}")
foreach(_cfg Debug Release RelWithDebInfo MinSizeRel)
    set_target_properties(media-format-core PROPERTIES
        RUNTIME_OUTPUT_DIRECTORY_${_cfg} "${MEDIA_FORMAT_RUNTIME_DIR}"
        LIBRARY_OUTPUT_DIRECTORY_${_cfg} "${MEDIA_FORMAT_RUNTIME_DIR}")
endforeach()
target_include_directories(media-format-core PUBLIC
    "$<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>"
    "$<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include/media-format>"
    "$<INSTALL_INTERFACE:include>"
    "$<INSTALL_INTERFACE:include/media-format>")
target_include_directories(media-format-core PRIVATE "${RAPIDJSON_INCLUDE_DIR}")
find_package(Threads REQUIRED)
target_link_libraries(media-format-core PRIVATE ${CMAKE_DL_LIBS} Threads::Threads)
target_compile_definitions(media-format-core PRIVATE
    MEDIA_FORMAT_ENABLE_PYTHON_BRIDGE=$<BOOL:${MEDIA_FORMAT_ENABLE_PYTHON_BRIDGE}>)
if(MEDIA_FORMAT_ENABLE_PYTHON_BRIDGE)
    find_package(Python3 3.11 REQUIRED COMPONENTS Interpreter Development)
    target_link_libraries(media-format-core PRIVATE Python3::Python)
    install(FILES plugin/python/media_format_frame_runtime.py
        DESTINATION share/media-format-core/python)
endif()
include(cmake/prepare_ai_sdk.cmake)
include(cmake/ffmpeg.cmake)
if(APPLE)
    set_property(TARGET media-format-core PROPERTY INSTALL_RPATH "@loader_path")
elseif(UNIX)
    set_property(TARGET media-format-core PROPERTY INSTALL_RPATH "$ORIGIN")
endif()
if(WIN32)
    target_compile_definitions(media-format-core PRIVATE NOMINMAX WIN32_LEAN_AND_MEAN _CRT_SECURE_NO_WARNINGS)
endif()
if(CMAKE_CXX_COMPILER_ID STREQUAL "GNU" AND CMAKE_CXX_COMPILER_VERSION VERSION_LESS "9.0")
    target_link_libraries(media-format-core PRIVATE stdc++fs)
endif()

if(MEDIA_FORMAT_BUILD_TESTS)
    enable_testing()
    add_test(NAME media_format_boundary COMMAND "${CMAKE_COMMAND}"
        "-DSOURCE_DIR=${CMAKE_CURRENT_SOURCE_DIR}" -P "${CMAKE_CURRENT_SOURCE_DIR}/tests/media_boundary.cmake")
    add_executable(media_format_forward_test tests/forward_privacy_test.cpp)
    target_link_libraries(media_format_forward_test PRIVATE media-format::core)
    add_test(NAME media_format_forward COMMAND media_format_forward_test)
    add_executable(media_format_python_bridge_test tests/python_bridge_test.cpp)
    target_link_libraries(media_format_python_bridge_test PRIVATE media-format::core Threads::Threads)
    target_compile_definitions(media_format_python_bridge_test PRIVATE
        MEDIA_FORMAT_TEST_PYTHON_HOST=$<BOOL:${MEDIA_FORMAT_ENABLE_PYTHON_BRIDGE}>)
    if(MEDIA_FORMAT_ENABLE_PYTHON_BRIDGE)
        if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/tests/python_bridge_video.cpp")
            add_executable(media_format_python_video_probe EXCLUDE_FROM_ALL tests/python_bridge_video.cpp)
            target_link_libraries(media_format_python_video_probe PRIVATE media-format::core)
        endif()
        target_link_libraries(media_format_python_bridge_test PRIVATE Python3::Python)
        add_test(NAME media_format_python_bridge COMMAND media_format_python_bridge_test
            "${CMAKE_CURRENT_SOURCE_DIR}/plugin/python" "${CMAKE_CURRENT_SOURCE_DIR}/tests" "${Python3_EXECUTABLE}")
        add_test(NAME media_format_python_host COMMAND media_format_python_bridge_test
            "${CMAKE_CURRENT_SOURCE_DIR}/plugin/python" "${CMAKE_CURRENT_SOURCE_DIR}/tests" "${Python3_EXECUTABLE}" host)
        set_tests_properties(media_format_python_bridge media_format_python_host PROPERTIES
            TIMEOUT 60 ENVIRONMENT "PYTHONDONTWRITEBYTECODE=1")
    else()
        add_test(NAME media_format_python_disabled COMMAND media_format_python_bridge_test)
    endif()
    if(WIN32)
        add_custom_command(TARGET media_format_forward_test POST_BUILD
            COMMAND ${CMAKE_COMMAND} -E copy_if_different "$<TARGET_FILE:media-format-core>"
                "$<TARGET_FILE_DIR:media_format_forward_test>" VERBATIM)
        add_custom_command(TARGET media_format_python_bridge_test POST_BUILD
            COMMAND ${CMAKE_COMMAND} -E copy_if_different "$<TARGET_FILE:media-format-core>"
                "$<TARGET_FILE_DIR:media_format_python_bridge_test>" VERBATIM)
        if(TARGET media_format_python_video_probe)
            add_custom_command(TARGET media_format_python_video_probe POST_BUILD
                COMMAND ${CMAKE_COMMAND} -E copy_if_different "$<TARGET_FILE:media-format-core>"
                    "$<TARGET_FILE_DIR:media_format_python_video_probe>" VERBATIM)
        endif()
    endif()
endif()

install(TARGETS media-format-core
    RUNTIME DESTINATION bin LIBRARY DESTINATION bin ARCHIVE DESTINATION lib)
install(DIRECTORY include/media-format DESTINATION include)
