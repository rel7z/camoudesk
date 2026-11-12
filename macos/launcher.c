#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <libgen.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#ifndef CAMOUDESK_VENV_PYTHON
#define CAMOUDESK_VENV_PYTHON "/usr/bin/python3"
#endif

#ifndef CAMOUDESK_SRC_DIR
#define CAMOUDESK_SRC_DIR ""
#endif

static void resolve_resources_src(char *out, size_t out_sz) {
    char exe_buf[PATH_MAX];
    uint32_t size = sizeof(exe_buf);
    if (_NSGetExecutablePath(exe_buf, &size) == 0) {
        char real_exe[PATH_MAX];
        if (realpath(exe_buf, real_exe) != NULL) {
            char *macos_dir = dirname(real_exe);       /* .../Contents/MacOS */
            char *contents_dir = dirname(macos_dir);   /* .../Contents */
            snprintf(out, out_sz, "%s/Resources/src", contents_dir);
            if (access(out, F_OK) == 0) {
                return;
            }
        }
    }
    snprintf(out, out_sz, "%s", CAMOUDESK_SRC_DIR);
}

int main(int argc, char **argv) {
    (void)argc;
    (void)argv;

    setenv("PYTHONUTF8", "1", 1);
    setenv("PYWEBVIEW_GUI", "cocoa", 1);

    char resources_src[PATH_MAX];
    resolve_resources_src(resources_src, sizeof(resources_src));

    PyConfig config;
    PyConfig_InitPythonConfig(&config);
    PyStatus status = PyConfig_SetBytesString(&config, &config.program_name, CAMOUDESK_VENV_PYTHON);
    if (PyStatus_Exception(status)) {
        PyConfig_Clear(&config);
        fprintf(stderr, "CamouDesk: failed to configure Python runtime\n");
        return 1;
    }

    status = Py_InitializeFromConfig(&config);
    PyConfig_Clear(&config);
    if (PyStatus_Exception(status)) {
        fprintf(stderr, "CamouDesk: failed to initialize Python runtime\n");
        return 1;
    }

    PyObject *sys_path = PySys_GetObject("path");
    if (sys_path && resources_src[0] != '\0') {
        PyObject *py_src = PyUnicode_FromString(resources_src);
        if (py_src) {
            PyList_Insert(sys_path, 0, py_src);
            Py_DECREF(py_src);
        }
    }

    int rc = 0;
    PyObject *mod = PyImport_ImportModule("camoudesk.ui.app");
    if (!mod) {
        PyErr_Print();
        rc = 1;
    } else {
        PyObject *fn = PyObject_GetAttrString(mod, "main");
        if (!fn || !PyCallable_Check(fn)) {
            PyErr_Print();
            rc = 1;
        } else {
            PyObject *res = PyObject_CallNoArgs(fn);
            if (!res) {
                PyErr_Print();
                rc = 1;
            } else {
                Py_DECREF(res);
            }
        }
        Py_XDECREF(fn);
        Py_DECREF(mod);
    }

    Py_Finalize();
    return rc;
}
