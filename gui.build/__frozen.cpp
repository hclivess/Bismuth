// This provides the frozen (compiled bytecode) files that are included if
// any.
#include <Python.h>

// Blob from which modules are unstreamed.
#if defined(_WIN32) && defined(_NUITKA_EXE)
extern const unsigned char* constant_bin;
#else
extern "C" const unsigned char constant_bin[];
#endif

#define stream_data constant_bin

// These modules should be loaded as bytecode. They may e.g. have to be loadable
// during "Py_Initialize" already, or for irrelevance, they are only included
// in this un-optimized form. These are not compiled by Nuitka, and therefore
// are not accelerated at all, merely bundled with the binary or module, so
// that CPython library can start out finding them.

void copyFrozenModulesTo( void* destination )
{
    _frozen frozen_modules[] = {

        { NULL, NULL, 0 }
    };

    memcpy(
        destination,
        frozen_modules,
        ( _NUITKA_FROZEN + 1 ) * sizeof( struct _frozen )
    );
}
