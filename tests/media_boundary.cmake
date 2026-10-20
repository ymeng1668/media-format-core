# The shared library must remain independent of its camera-protocol consumer.
file(GLOB_RECURSE sources
    "${SOURCE_DIR}/src/*.cpp" "${SOURCE_DIR}/include/*.h"
    "${SOURCE_DIR}/third_party/*.cpp")
foreach(source IN LISTS sources)
    file(READ "${source}" contents)
    string(TOLOWER "${contents}" lower)
    if(lower MATCHES "n9m|media_extractor|requestdownloadvideo|ctrlrecord|openssl/|aes_decrypt")
        message(FATAL_ERROR "Protocol-specific implementation found in ${source}")
    endif()
endforeach()
if(EXISTS "${SOURCE_DIR}/app")
    message(FATAL_ERROR "The media core must not contain an application")
endif()
