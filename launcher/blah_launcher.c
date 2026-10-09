/*
 * Blah.app's main executable.
 *
 * Runs Python inside this process (instead of handing off to a separate Python
 * program), so macOS sees Blah as one app: the Dock, ⌘Tab and permissions like
 * Accessibility and Input Monitoring all apply to "Blah".
 *
 * install.sh fills in the three paths below and compiles this file.
 */
#include <Python.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <mach-o/dyld.h>
#include <limits.h>
#include <sys/stat.h>
#include <unistd.h>

#define BLAH_PYTHON_HOME "@PYTHON_HOME@"
#define BLAH_SITE_PACKAGES "@SITE_PACKAGES@"
#define BLAH_SRC "@SRC@"
#define BLAH_PYTHON "@PYTHON@"

int main(int argc, char *argv[]) {
    /* Where is Blah.app? (…/Blah.app/Contents/MacOS/Blah) */
    char exe[PATH_MAX], real[PATH_MAX];
    uint32_t size = sizeof(exe);
    if (_NSGetExecutablePath(exe, &size) == 0 && realpath(exe, real) != NULL) {
        /* Trim "/Contents/MacOS/Blah" to get …/Blah.app */
        for (int up = 0; up < 3; up++) {
            char *slash = strrchr(real, '/');
            if (slash == NULL || slash == real) break;
            *slash = '\0';
        }
        setenv("BLAH_APP", real, 1);
    }

    char pythonpath[PATH_MAX * 2];
    snprintf(pythonpath, sizeof(pythonpath), "%s:%s", BLAH_SRC, BLAH_SITE_PACKAGES);
    setenv("PYTHONPATH", pythonpath, 1);
    setenv("PYTHONHOME", BLAH_PYTHON_HOME, 1);
    setenv("PYTHONNOUSERSITE", "1", 1);
    setenv("PYTHONDONTWRITEBYTECODE", "1", 1);
    setenv("BLAH_PYTHON", BLAH_PYTHON, 1);  /* real Python, for helper processes */

    /* Send output to ~/.blah/blah.log */
    const char *home = getenv("HOME");
    if (home) {
        char dir[PATH_MAX], log[PATH_MAX];
        snprintf(dir, sizeof(dir), "%s/.blah", home);
        mkdir(dir, 0700);
        snprintf(log, sizeof(log), "%s/blah.log", dir);
        if (freopen(log, "a", stderr)) setvbuf(stderr, NULL, _IOLBF, 0);
        freopen(log, "a", stdout);
        chdir(dir);
    }

    /* python -m blah [args…] */
    char **args = calloc((size_t)argc + 3, sizeof(char *));
    if (args == NULL) return 1;
    int count = 0;
    args[count++] = argv[0];
    args[count++] = "-m";
    args[count++] = "blah";
    for (int i = 1; i < argc; i++) {
        if (strncmp(argv[i], "-psn_", 5) == 0) continue;  /* old macOS process-serial argument */
        args[count++] = argv[i];
    }
    args[count] = NULL;
    return Py_BytesMain(count, args);
}
