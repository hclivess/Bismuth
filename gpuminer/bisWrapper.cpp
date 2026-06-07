#include <iostream>
#include <pybind11/pybind11.h>
#include <pybind11/numpy.h>
#include <pybind11/stl.h>
#include <cuda_runtime.h>
#include <string>
#include "bis.h"

namespace py = pybind11;
PYBIND11_MODULE(bis, m) {
        py::class_<BisCuda>(m, "BisCuda")
        .def(py::init())
        .def("ValidNoncesAvailable", &BisCuda::ValidNoncesAvailable)
        .def("ValidNoncesGet", &BisCuda::ValidNoncesGet)
        .def("Update", &BisCuda::Update)
        .def("GetNumerOfHashesSinceLastCall", &BisCuda::GetNumerOfHashesSinceLastCall);
}
