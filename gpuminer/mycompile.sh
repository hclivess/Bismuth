#!/bin/bash
export CC=/usr/bin/gcc-8
export CXX=/usr/bin/g++-8
rm -rf CMakeFiles
rm CMakeCache.txt
cmake .
make
