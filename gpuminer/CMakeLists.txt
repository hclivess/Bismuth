cmake_minimum_required(VERSION 3.5)
find_package(CUDA)
find_package(PythonInterp REQUIRED)
find_package(PythonLibs 3.8 REQUIRED)

include_directories(${PYTHON_INCLUDE_DIRS})

if(UNIX)
    SET(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -Wall -std=c++11 -O3")
endif()

set(CUDA_NVCC_FLAGS ${CUDA_NVCC_FLAGS};--disable-warnings;--ptxas-options=-v;-use_fast_math;-lineinfo)

# Set the architecture of your CUDA card
set(CUDA_NVCC_FLAGS ${CUDA_NVCC_FLAGS};-gencode=arch=compute_61,code=sm_61)


list(APPEND CUDA_NVCC_FLAGS_RELEASE)

cuda_add_library(bis SHARED
  bisWrapper.cpp
  bis.cu)

target_link_libraries(bis
  ${PYTHON_LIBRARIES}
  ${CUDA_LIBRARIES})

set_target_properties(bis PROPERTIES PREFIX "")
