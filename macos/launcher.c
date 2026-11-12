#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <libgen.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/*
 * Runtime resolution order:
 *   1. Contents/Resources/python  -- relocatable CPython bundled by build_app.sh (default build)
 *   2. CAMOUDESK_VENV_PYTHON      -- the build machine's interpreter (`build_app.sh --dev` only)
 */
#ifndef CAMOUDESK_VENV_PYTHON
#define CAMOUDESK_VENV_PYTHON ""
#endif

#ifndef CAMOUDESK_SRC_DIR
#define CAMOUDESK_SRC_DIR ""
#endif

static int resolve_contents_dir(char *out, size_t out_sz) {
    char exe_buf[PATH_MAX];
    uint32_t size = sizeof(exe_buf);
    char real_exe[PATH_MAX];
    if (_NSGetExecutablePath(exe_buf, &size) != 0 || realpath(exe_buf, real_exe) == NULL) {
        return 0;
    }
    char *macos_dir = dirname(real_exe);     /* .../Contents/MacOS */
    char *contents_dir = dirname(macos_dir); /* .../Contents */
    snprintf(out, out_sz, "%s", contents_dir);
    return 1;
}

static void fail(const char *msg) {
    fprintf(stderr, "CamouDesk: %s\n", msg);
}

int main(int argc, char **argv) {
    (void)argc;
    (void)argv;

    setenv("PYTHONUTF8", "1", 1);
    setenv("PYWEBVIEW_GUI", "cocoa", 1);

    char contents[PATH_MAX] = "";
    char bundled_home[PATH_MAX] = "";
    char bundled_python[PATH_MAX] = "";
    char resources_src[PATH_MAX] = "";
    if (resolve_contents_dir(contents, sizeof(contents))) {
        snprintf(bundled_home, sizeof(bundled_home), "%s/Resources/python", contents);
        snprintf(bundled_python, sizeof(bundled_python), "%s/bin/python3", bundled_home);
        snprintf(resources_src, sizeof(resources_src), "%s/Resources/src", contents);
        if (access(resources_src, F_OK) != 0) {
            resources_src[0] = '\0';
        }
    }
    int use_bundled = bundled_python[0] != '\0' && access(bundled_python, X_OK) == 0;
    if (!use_bundled && CAMOUDESK_SRC_DIR[0] != '\0' && resources_src[0] == '\0') {
        snprintf(resources_src, sizeof(resources_src), "%s", CAMOUDESK_SRC_DIR);
    }

    const char *program = use_bundled ? bundled_python : CAMOUDESK_VENV_PYTHON;
    if (program[0] == '\0') {
        fail("no Python runtime found in Contents/Resources/python; rebuild with macos/build_app.sh");
        return 1;
    }

    PyConfig config;
    /* Isolated: ignore PYTHONPATH/PYTHONHOME and user site-packages from the launching environment. */
    PyConfig_InitIsolatedConfig(&config);
    if (use_bundled) {
        /* Never write .pyc files into the signed bundle (it would break the code seal). The build
           precompiles everything; children inherit the variable and workers also run with -B. */
        config.write_bytecode = 0;
        setenv("PYTHONDONTWRITEBYTECODE", "1", 1);
    }
    PyStatus status = PyConfig_SetBytesString(&config, &config.program_name, program);
    if (!PyStatus_Exception(status) && use_bundled) {
        status = PyConfig_SetBytesString(&config, &config.home, bundled_home);
    }
    if (PyStatus_Exception(status)) {
        PyConfig_Clear(&config);
        fail("failed to configure Python runtime");
        return 1;
    }

    status = Py_InitializeFromConfig(&config);
    PyConfig_Clear(&config);
    if (PyStatus_Exception(status)) {
        fail("failed to initialize Python runtime");
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
