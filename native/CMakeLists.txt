# SPDX-License-Identifier: GPL-3.0-or-later

add_library(haori_cosserat SHARED
    src/c_api.cpp
    src/cuda_solver.cu
)

target_include_directories(haori_cosserat
    PUBLIC
        ${CMAKE_CURRENT_SOURCE_DIR}/include
    PRIVATE
        ${CMAKE_CURRENT_SOURCE_DIR}/src
)

target_compile_features(haori_cosserat PUBLIC cxx_std_20)
target_compile_definitions(haori_cosserat PRIVATE HSC_BUILD_DLL)
set_target_properties(haori_cosserat PROPERTIES
    CUDA_STANDARD 20
    CUDA_STANDARD_REQUIRED ON
    CUDA_RUNTIME_LIBRARY Static
    CUDA_ARCHITECTURES "75;80;86;89;90;100;120"
    MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>"
)

if(MSVC)
    target_compile_options(haori_cosserat PRIVATE
        $<$<COMPILE_LANGUAGE:CXX>:/W4;/permissive-;/EHsc;/utf-8>
    )
else()
    target_compile_options(haori_cosserat PRIVATE -Wall -Wextra -Wpedantic)
endif()

set_target_properties(haori_cosserat PROPERTIES
    OUTPUT_NAME "haori_cosserat"
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin"
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin"
    ARCHIVE_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/lib"
)

install(TARGETS haori_cosserat
    RUNTIME DESTINATION bin
    LIBRARY DESTINATION bin
)

if(BUILD_TESTING)
    add_executable(haori_cosserat_tests tests/test_solver.cpp)
    target_link_libraries(haori_cosserat_tests PRIVATE haori_cosserat)
    target_compile_features(haori_cosserat_tests PRIVATE cxx_std_20)
    if(MSVC)
        target_compile_options(haori_cosserat_tests PRIVATE /W4 /permissive- /EHsc /utf-8)
    else()
        target_compile_options(haori_cosserat_tests PRIVATE -Wall -Wextra -Wpedantic)
    endif()
    set_target_properties(haori_cosserat_tests PROPERTIES
        RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin"
        MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>"
    )
    add_test(NAME haori_cosserat_tests COMMAND haori_cosserat_tests)
endif()
