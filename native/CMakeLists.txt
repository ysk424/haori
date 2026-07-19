# SPDX-License-Identifier: GPL-3.0-or-later

add_library(haori_cosserat SHARED
    src/c_api.cpp
    src/solver.cpp
)

if(MSVC)
    find_package(OpenMP REQUIRED COMPONENTS CXX)
    # MSVC OpenMP links VCOMP140.DLL dynamically. Install the licensed x64
    # redistributable beside the solver so a portable Blender installation
    # does not depend on an unrelated system-wide VC runtime installation.
    set(CMAKE_INSTALL_OPENMP_LIBRARIES TRUE)
    set(CMAKE_INSTALL_SYSTEM_RUNTIME_LIBS_SKIP TRUE)
    include(InstallRequiredSystemLibraries)
    set(HSC_OPENMP_RUNTIME_LIBS "")
    foreach(runtime_library IN LISTS CMAKE_INSTALL_SYSTEM_RUNTIME_LIBS)
        if(runtime_library MATCHES "[/\\\\]vcomp140\\.dll$")
            list(APPEND HSC_OPENMP_RUNTIME_LIBS "${runtime_library}")
        endif()
    endforeach()
    if(NOT HSC_OPENMP_RUNTIME_LIBS)
        message(FATAL_ERROR "MSVC OpenMP runtime vcomp140.dll was not found")
    endif()
    install(PROGRAMS ${HSC_OPENMP_RUNTIME_LIBS} DESTINATION bin)
endif()

target_include_directories(haori_cosserat
    PUBLIC
        ${CMAKE_CURRENT_SOURCE_DIR}/include
    PRIVATE
        ${CMAKE_CURRENT_SOURCE_DIR}/src
)

target_compile_features(haori_cosserat PUBLIC cxx_std_20)
target_compile_definitions(haori_cosserat PRIVATE HSC_BUILD_DLL)
if(MSVC)
    target_link_libraries(haori_cosserat PRIVATE OpenMP::OpenMP_CXX)
endif()

if(MSVC)
    target_compile_options(haori_cosserat PRIVATE /W4 /permissive- /EHsc /utf-8)
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
    )
    add_test(NAME haori_cosserat_tests COMMAND haori_cosserat_tests)
endif()
