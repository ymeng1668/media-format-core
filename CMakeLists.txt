cmake_minimum_required(VERSION 3.16)
project(media_format_core VERSION 0.1.0 LANGUAGES CXX)

if(CMAKE_SOURCE_DIR STREQUAL CMAKE_CURRENT_SOURCE_DIR)
    include(CTest)
endif()
option(MEDIA_FORMAT_BUILD_TESTS "Build shared media-format regression tests" ${BUILD_TESTING})
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
target_link_libraries(media-format-core PRIVATE ${CMAKE_DL_LIBS})
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
    if(WIN32)
        add_custom_command(TARGET media_format_forward_test POST_BUILD
            COMMAND ${CMAKE_COMMAND} -E copy_if_different "$<TARGET_FILE:media-format-core>"
                "$<TARGET_FILE_DIR:media_format_forward_test>" VERBATIM)
    endif()
endif()

install(TARGETS media-format-core
    RUNTIME DESTINATION bin LIBRARY DESTINATION bin ARCHIVE DESTINATION lib)
install(DIRECTORY include/media-format DESTINATION include)
