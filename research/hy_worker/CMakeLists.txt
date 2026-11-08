cmake_minimum_required(VERSION 3.20)
project(a2a_hy_worker LANGUAGES CXX)

set(LLAMA_ROOT "" CACHE PATH "Pinned Hy-SimulMT llama.cpp fork checkout")
if(NOT LLAMA_ROOT)
  message(FATAL_ERROR "Pass -DLLAMA_ROOT=/path/to/pinned/llama.cpp")
endif()

add_executable(hy-worker hy_worker.cpp)
target_compile_features(hy-worker PRIVATE cxx_std_17)
target_include_directories(hy-worker PRIVATE
  "${LLAMA_ROOT}/include"
  "${LLAMA_ROOT}/ggml/include"
  "${LLAMA_ROOT}/tools/mtmd"
  "${LLAMA_ROOT}/vendor")
target_link_directories(hy-worker PRIVATE
  "${LLAMA_ROOT}/build/bin"
  "${LLAMA_ROOT}/build/bin/Release"
  "${LLAMA_ROOT}/build/src/Release"
  "${LLAMA_ROOT}/build/common/Release"
  "${LLAMA_ROOT}/build/ggml/src/Release"
  "${LLAMA_ROOT}/build/tools/mtmd/Release")
target_link_libraries(hy-worker PRIVATE llama mtmd ggml ggml-base)
if(MSVC)
  target_compile_options(hy-worker PRIVATE /W4 /EHsc)
else()
  target_compile_options(hy-worker PRIVATE -O2 -Wall)
  set_target_properties(hy-worker PROPERTIES BUILD_RPATH "${LLAMA_ROOT}/build/bin")
endif()
set_target_properties(hy-worker PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/../../.local/hy-simulmt/bin"
  RUNTIME_OUTPUT_DIRECTORY_RELEASE "${CMAKE_CURRENT_SOURCE_DIR}/../../.local/hy-simulmt/bin")
