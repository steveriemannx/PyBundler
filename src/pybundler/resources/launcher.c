/*
 * PyBundler native launcher.
 *
 * Compiled once per application.  Responsibilities:
 *   1. locate itself (and, for onefile builds, extract the appended payload
 *      into a per-user cache directory exactly once),
 *   2. point the bundled interpreter at the bundled stdlib and application,
 *   3. exec the interpreter with the bootstrap module.
 *
 * Build-time configuration is injected with -D flags, so this file never
 * contains application specific data and can be cached by the compiler.
 *
 * Written in C99 with no dependencies beyond libc and (optionally) zlib.
 */

#if defined(_MSC_VER)
#define _CRT_SECURE_NO_WARNINGS
#pragma comment(lib, "shell32.lib")
#pragma comment(lib, "user32.lib")
#endif

#if !defined(_WIN32)
#define _POSIX_C_SOURCE 200809L
#endif

#include <errno.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#if defined(_WIN32)
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <direct.h>
#include <io.h>
#include <process.h>
#include <shellapi.h>
#include <sys/stat.h>
#include <wchar.h>
#else
#include <dirent.h>
#include <fcntl.h>
#include <limits.h>
#include <signal.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>
#if defined(__APPLE__)
#include <mach-o/dyld.h>
#endif
#if defined(__FreeBSD__)
#include <sys/param.h>
#include <sys/sysctl.h>
#endif
#endif

#if PYB_USE_ZLIB
#include <zlib.h>
#endif

#ifndef PYB_NAME
#define PYB_NAME "app"
#endif
#ifndef PYB_BUILD_ID
#define PYB_BUILD_ID "000000000000"
#endif
#ifndef PYB_MODE_ONEFILE
#define PYB_MODE_ONEFILE 0
#endif
#ifndef PYB_HOME_REL
#define PYB_HOME_REL "runtime/python"
#endif
#ifndef PYB_EXE_REL
#define PYB_EXE_REL "runtime/python/bin/python3"
#endif
#ifndef PYB_SITE_REL
#define PYB_SITE_REL "site"
#endif
#ifndef PYB_LIB_REL
#define PYB_LIB_REL "lib"
#endif
#ifndef PYB_WINDOWED
#define PYB_WINDOWED 0
#endif
#ifndef PYB_CONSOLE
#define PYB_CONSOLE 1
#endif

#if defined(_WIN32)
#define pyb_fseek _fseeki64
#define pyb_ftell _ftelli64
#else
#define pyb_fseek fseek
#define pyb_ftell ftell
#endif
typedef int64_t pyb_offset_t;

#define PYB_CONTAINER_MAGIC "PBFC1\r\n\0"
#define PYB_FOOTER_MAGIC "PBFEND01"
#define PYB_FOOTER_SIZE 24
#define PYB_MAX_PATH 8192
#define PYB_PATH_SEP '/'

/* ------------------------------------------------------------------ */
/* Diagnostics                                                        */
/* ------------------------------------------------------------------ */

static void pyb_show_error(const char *message) {
#if defined(_WIN32) && !PYB_CONSOLE
    wchar_t wide[2048];
    MultiByteToWideChar(CP_UTF8, 0, message, -1, wide, 2048);
    MessageBoxW(NULL, wide, L"Application error", MB_ICONERROR | MB_OK);
#endif
    fprintf(stderr, "pybundler: %s\n", message);
    fflush(stderr);
}

static void pyb_die(const char *format, ...) {
    char buffer[4096];
    va_list args;
    va_start(args, format);
    vsnprintf(buffer, sizeof(buffer), format, args);
    va_end(args);
    pyb_show_error(buffer);
    exit(1);
}

static void *pyb_alloc(size_t size) {
    void *pointer = malloc(size ? size : 1);
    if (pointer == NULL) {
        pyb_die("out of memory while launching the application");
    }
    return pointer;
}

static char *pyb_strdup(const char *value) {
    size_t length = strlen(value);
    char *copy = (char *)pyb_alloc(length + 1);
    memcpy(copy, value, length + 1);
    return copy;
}

static char *pyb_join2(const char *a, const char *b) {
    size_t length = strlen(a) + strlen(b) + 2;
    char *result = (char *)pyb_alloc(length);
    if (b[0] == '\0') {
        snprintf(result, length, "%s", a);
    } else {
        snprintf(result, length, "%s%c%s", a, PYB_PATH_SEP, b);
    }
    return result;
}

static char *pyb_join3(const char *a, const char *b, const char *c) {
    char *first = pyb_join2(a, b);
    char *second = pyb_join2(first, c);
    free(first);
    return second;
}

static char *pyb_dirname(const char *path) {
    char *copy = pyb_strdup(path);
    char *slash = strrchr(copy, PYB_PATH_SEP);
#if defined(_WIN32)
    char *backslash = strrchr(copy, '\\');
    if (backslash != NULL && (slash == NULL || backslash > slash)) {
        slash = backslash;
    }
#endif
    if (slash == NULL) {
        free(copy);
        return pyb_strdup(".");
    }
    if (slash == copy) {
        slash[1] = '\0';
    } else {
        *slash = '\0';
    }
    return copy;
}

/* ------------------------------------------------------------------ */
/* Platform wrappers (UTF-8 paths)                                    */
/* ------------------------------------------------------------------ */

#if defined(_WIN32)

static wchar_t *pyb_wide(const char *text) {
    int needed = MultiByteToWideChar(CP_UTF8, 0, text, -1, NULL, 0);
    if (needed <= 0) {
        return NULL;
    }
    wchar_t *wide = (wchar_t *)pyb_alloc((size_t)needed * sizeof(wchar_t));
    MultiByteToWideChar(CP_UTF8, 0, text, -1, wide, needed);
    return wide;
}

static char *pyb_narrow(const wchar_t *text) {
    int needed = WideCharToMultiByte(CP_UTF8, 0, text, -1, NULL, 0, NULL, NULL);
    if (needed <= 0) {
        return NULL;
    }
    char *narrow = (char *)pyb_alloc((size_t)needed);
    WideCharToMultiByte(CP_UTF8, 0, text, -1, narrow, needed, NULL, NULL);
    return narrow;
}

static int pyb_is_dir(const char *path) {
    wchar_t *wide = pyb_wide(path);
    struct _stat64 info;
    int result = (wide != NULL && _wstat64(wide, &info) == 0 &&
                  (info.st_mode & _S_IFDIR) != 0);
    free(wide);
    return result;
}

static int pyb_is_file(const char *path) {
    wchar_t *wide = pyb_wide(path);
    struct _stat64 info;
    int result = (wide != NULL && _wstat64(wide, &info) == 0 &&
                  (info.st_mode & _S_IFREG) != 0);
    free(wide);
    return result;
}

static int pyb_mkdir(const char *path) {
    wchar_t *wide = pyb_wide(path);
    int result = (wide != NULL) ? _wmkdir(wide) : -1;
    free(wide);
    return result;
}

static FILE *pyb_fopen(const char *path, const char *mode) {
    wchar_t *wide_path = pyb_wide(path);
    wchar_t *wide_mode = pyb_wide(mode);
    FILE *handle = (wide_path != NULL && wide_mode != NULL)
                       ? _wfopen(wide_path, wide_mode)
                       : NULL;
    free(wide_path);
    free(wide_mode);
    return handle;
}

static int pyb_chmod(const char *path, int mode) {
    wchar_t *wide = pyb_wide(path);
    int result = (wide != NULL) ? _wchmod(wide, mode) : -1;
    free(wide);
    return result;
}

static int pyb_setenv(const char *name, const char *value) {
    wchar_t *wide_name = pyb_wide(name);
    wchar_t *wide_value = pyb_wide(value);
    int result = (wide_name != NULL && wide_value != NULL)
                     ? _wputenv_s(wide_name, wide_value)
                     : -1;
    free(wide_name);
    free(wide_value);
    return result;
}

static int pyb_rename_dir(const char *from, const char *to) {
    wchar_t *wide_from = pyb_wide(from);
    wchar_t *wide_to = pyb_wide(to);
    int ok = 0;
    if (wide_from != NULL && wide_to != NULL) {
        ok = MoveFileExW(wide_from, wide_to,
                         MOVEFILE_REPLACE_EXISTING | MOVEFILE_COPY_ALLOWED);
    }
    free(wide_from);
    free(wide_to);
    return ok ? 0 : -1;
}

static int pyb_remove_dir_tree(const char *path) {
    wchar_t *wide = pyb_wide(path);
    if (wide == NULL) {
        return -1;
    }
    size_t length = wcslen(wide);
    wchar_t *list = (wchar_t *)pyb_alloc((length + 2) * sizeof(wchar_t));
    memcpy(list, wide, (length + 1) * sizeof(wchar_t));
    list[length + 1] = L'\0';
    SHFILEOPSTRUCTW operation;
    memset(&operation, 0, sizeof(operation));
    operation.wFunc = FO_DELETE;
    operation.pFrom = list;
    operation.fFlags = FOF_SILENT | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_NOCONFIRMMKDIR;
    int result = SHFileOperationW(&operation);
    free(list);
    free(wide);
    return result == 0 ? 0 : -1;
}

#else /* POSIX */

static int pyb_is_dir(const char *path) {
    struct stat info;
    return stat(path, &info) == 0 && S_ISDIR(info.st_mode);
}

static int pyb_is_file(const char *path) {
    struct stat info;
    return stat(path, &info) == 0 && S_ISREG(info.st_mode);
}

static int pyb_mkdir(const char *path) {
    return mkdir(path, 0777);
}

static FILE *pyb_fopen(const char *path, const char *mode) {
    return fopen(path, mode);
}

static int pyb_chmod(const char *path, int mode) {
    return chmod(path, (mode_t)mode);
}

static int pyb_setenv(const char *name, const char *value) {
    return setenv(name, value, 1);
}

static int pyb_rename_dir(const char *from, const char *to) {
    return rename(from, to);
}

static int pyb_remove_dir_tree(const char *path) {
    struct stat info;
    if (lstat(path, &info) != 0) {
        return -1;
    }
    if (!S_ISDIR(info.st_mode)) {
        return unlink(path);
    }
    DIR *directory = opendir(path);
    if (directory == NULL) {
        return -1;
    }
    struct dirent *entry;
    while ((entry = readdir(directory)) != NULL) {
        if (strcmp(entry->d_name, ".") == 0 || strcmp(entry->d_name, "..") == 0) {
            continue;
        }
        char *child = pyb_join2(path, entry->d_name);
        pyb_remove_dir_tree(child);
        free(child);
    }
    closedir(directory);
    return rmdir(path);
}

#endif /* _WIN32 */

static int pyb_mkdirs(const char *path) {
    char buffer[PYB_MAX_PATH];
    size_t length = strlen(path);
    if (length == 0 || length >= sizeof(buffer)) {
        return -1;
    }
    memcpy(buffer, path, length + 1);
    size_t start = 0;
#if defined(_WIN32)
    if (length > 2 && buffer[1] == ':') {
        start = 3;
    }
#endif
    for (size_t index = start; index <= length; index++) {
        char character = buffer[index];
        if (character == PYB_PATH_SEP
#if defined(_WIN32)
            || character == '\\'
#endif
            || character == '\0') {
            buffer[index] = '\0';
            if (buffer[0] != '\0' && !pyb_is_dir(buffer)) {
                pyb_mkdir(buffer);
            }
            buffer[index] = character;
        }
    }
    return pyb_is_dir(path) ? 0 : -1;
}

/* ------------------------------------------------------------------ */
/* Locating ourselves and the cache                                   */
/* ------------------------------------------------------------------ */

static char *pyb_self_path(const char *argv0) {
#if defined(_WIN32)
    wchar_t wide[PYB_MAX_PATH];
    DWORD length = GetModuleFileNameW(NULL, wide, PYB_MAX_PATH);
    if (length > 0 && length < PYB_MAX_PATH) {
        char *narrow = pyb_narrow(wide);
        if (narrow != NULL) {
            return narrow;
        }
    }
#elif defined(__APPLE__)
    uint32_t size = 0;
    _NSGetExecutablePath(NULL, &size);
    char *buffer = (char *)pyb_alloc(size + 1);
    if (_NSGetExecutablePath(buffer, &size) == 0) {
        char resolved[PATH_MAX];
        if (realpath(buffer, resolved) != NULL) {
            free(buffer);
            return pyb_strdup(resolved);
        }
        return buffer;
    }
    free(buffer);
#elif defined(__linux__)
    char buffer[PATH_MAX];
    ssize_t length = readlink("/proc/self/exe", buffer, sizeof(buffer) - 1);
    if (length > 0) {
        buffer[length] = '\0';
        return pyb_strdup(buffer);
    }
#elif defined(__FreeBSD__)
    {
        int mib[4] = {CTL_KERN, KERN_PROC, KERN_PROC_PATHNAME, -1};
        char buffer[PATH_MAX];
        size_t length = sizeof(buffer);
        if (sysctl(mib, 4, buffer, &length, NULL, 0) == 0 && length > 1) {
            if (length >= sizeof(buffer)) {
                length = sizeof(buffer) - 1;
            }
            buffer[length] = '\0';
            char resolved[PATH_MAX];
            if (realpath(buffer, resolved) != NULL) {
                return pyb_strdup(resolved);
            }
            return pyb_strdup(buffer);
        }
    }
#endif
#if defined(_WIN32)
    (void)argv0;
    pyb_die("cannot determine the location of the application executable");
    return NULL;
#else
    if (argv0 != NULL && argv0[0] != '\0') {
        char resolved[PATH_MAX];
        if (realpath(argv0, resolved) != NULL) {
            return pyb_strdup(resolved);
        }
        return pyb_strdup(argv0);
    }
    pyb_die("cannot determine the location of the application executable");
    return NULL;
#endif
}

static char *pyb_cache_dir(void) {
    const char *override = getenv("PYBUNDLER_CACHE_DIR");
    char *base = NULL;
    if (override != NULL && override[0] != '\0') {
        base = pyb_strdup(override);
    } else {
#if defined(_WIN32)
        const wchar_t *local_wide = _wgetenv(L"LOCALAPPDATA");
        if (local_wide != NULL) {
            char *local = pyb_narrow(local_wide);
            if (local != NULL && local[0] != '\0') {
                base = local;
            } else {
                free(local);
            }
        }
#elif defined(__APPLE__)
        const char *home = getenv("HOME");
        if (home != NULL && home[0] != '\0') {
            base = pyb_join3(home, "Library", "Caches");
        }
#else
        const char *xdg = getenv("XDG_CACHE_HOME");
        if (xdg != NULL && xdg[0] != '\0') {
            base = pyb_strdup(xdg);
        } else {
            const char *home = getenv("HOME");
            if (home != NULL && home[0] != '\0') {
                base = pyb_join2(home, ".cache");
            }
        }
#endif
    }
    if (base == NULL) {
        base = pyb_strdup(".");
    }
    char *root = pyb_join2(base, "pybundler");
    free(base);
    return root;
}

/* ------------------------------------------------------------------ */
/* Endian helpers                                                     */
/* ------------------------------------------------------------------ */

static uint16_t pyb_rd16(const unsigned char *p) {
    return (uint16_t)(p[0] | ((uint16_t)p[1] << 8));
}

static uint32_t pyb_rd32(const unsigned char *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) |
           ((uint32_t)p[3] << 24);
}

static uint64_t pyb_rd64(const unsigned char *p) {
    uint64_t low = pyb_rd32(p);
    uint64_t high = pyb_rd32(p + 4);
    return low | (high << 32);
}

/* ------------------------------------------------------------------ */
/* Onefile container                                                  */
/* ------------------------------------------------------------------ */

static int pyb_read_footer(const char *self, uint64_t *offset, uint64_t *length) {
    FILE *handle = pyb_fopen(self, "rb");
    if (handle == NULL) {
        return -1;
    }
    if (pyb_fseek(handle, 0, SEEK_END) != 0) {
        fclose(handle);
        return -1;
    }
    pyb_offset_t size = pyb_ftell(handle);
    if (size < PYB_FOOTER_SIZE) {
        fclose(handle);
        return -1;
    }
    if (pyb_fseek(handle, size - PYB_FOOTER_SIZE, SEEK_SET) != 0) {
        fclose(handle);
        return -1;
    }
    unsigned char footer[PYB_FOOTER_SIZE];
    if (fread(footer, 1, PYB_FOOTER_SIZE, handle) != PYB_FOOTER_SIZE) {
        fclose(handle);
        return -1;
    }
    fclose(handle);
    if (memcmp(footer, PYB_FOOTER_MAGIC, 8) != 0) {
        return -1;
    }
    *offset = pyb_rd64(footer + 8);
    *length = pyb_rd64(footer + 16);
    if (*offset == 0 || *length == 0 || (uint64_t)size < *offset + *length) {
        return -1;
    }
    return 0;
}

static int pyb_safe_name(const char *name) {
    if (name[0] == '\0') {
        return 0;
    }
    if (name[0] == '/' || name[0] == '\\') {
        return 0;
    }
    if (strlen(name) > 1 && name[1] == ':') {
        return 0;
    }
    if (strchr(name, '\\') != NULL) {
        return 0;
    }
    const char *cursor = name;
    while (*cursor != '\0') {
        const char *segment = cursor;
        while (*cursor != '\0' && *cursor != '/') {
            cursor++;
        }
        size_t length = (size_t)(cursor - segment);
        if (length == 0) {
            return 0;
        }
        if (length == 2 && segment[0] == '.' && segment[1] == '.') {
            return 0;
        }
        if (*cursor == '/') {
            cursor++;
            if (*cursor == '\0') {
                return 0;
            }
        }
    }
    return 1;
}

static int pyb_inflate_to_file(const unsigned char *source, uint64_t source_length,
                               FILE *output, uint64_t expected) {
#if PYB_USE_ZLIB
    z_stream stream;
    memset(&stream, 0, sizeof(stream));
    if (inflateInit(&stream) != Z_OK) {
        return -1;
    }
    stream.next_in = (Bytef *)source;
    stream.avail_in = (uInt)source_length;
    unsigned char buffer[65536];
    uint64_t total = 0;
    int status;
    do {
        stream.next_out = buffer;
        stream.avail_out = (uInt)sizeof(buffer);
        status = inflate(&stream, Z_NO_FLUSH);
        if (status != Z_OK && status != Z_STREAM_END) {
            inflateEnd(&stream);
            return -1;
        }
        size_t produced = sizeof(buffer) - stream.avail_out;
        if (produced > 0 && fwrite(buffer, 1, produced, output) != produced) {
            inflateEnd(&stream);
            return -1;
        }
        total += produced;
    } while (status != Z_STREAM_END);
    inflateEnd(&stream);
    return total == expected ? 0 : -1;
#else
    (void)source;
    (void)source_length;
    (void)output;
    (void)expected;
    return -1;
#endif
}

static void pyb_extract(const char *self, uint64_t offset, uint64_t length,
                        const char *destination) {
    FILE *handle = pyb_fopen(self, "rb");
    if (handle == NULL) {
        pyb_die("cannot open %s: %s", self, strerror(errno));
    }
    if (pyb_fseek(handle, (pyb_offset_t)offset, SEEK_SET) != 0) {
        fclose(handle);
        pyb_die("cannot seek to the application payload");
    }
    if (length > (uint64_t)SIZE_MAX) {
        fclose(handle);
        pyb_die("this onefile payload is too large for a 32-bit launcher");
    }
    unsigned char *payload = (unsigned char *)pyb_alloc((size_t)length);
    size_t read = fread(payload, 1, (size_t)length, handle);
    fclose(handle);
    if (read != (size_t)length) {
        pyb_die("application payload is truncated");
    }
    if (length < 16 || memcmp(payload, PYB_CONTAINER_MAGIC, 8) != 0) {
        pyb_die("application payload is corrupted (bad magic)");
    }
    uint32_t version = pyb_rd32(payload + 8);
    uint32_t count = pyb_rd32(payload + 12);
    if (version != 1) {
        pyb_die("unsupported application payload version %u", version);
    }
    const unsigned char *cursor = payload + 16;
    const unsigned char *end = payload + length;
    if (pyb_mkdirs(destination) != 0) {
        pyb_die("cannot create extraction directory %s", destination);
    }
    for (uint32_t index = 0; index < count; index++) {
        if ((size_t)(end - cursor) < 2 + 1 + 1 + 4 + 8 + 8) {
            pyb_die("application payload is truncated (entry %u)", index);
        }
        uint16_t name_length = pyb_rd16(cursor);
        cursor += 2;
        uint8_t method = *cursor++;
        uint8_t kind = *cursor++;
        uint32_t mode = pyb_rd32(cursor);
        cursor += 4;
        uint64_t uncompressed = pyb_rd64(cursor);
        cursor += 8;
        uint64_t compressed = pyb_rd64(cursor);
        cursor += 8;
        if ((size_t)(end - cursor) < name_length) {
            pyb_die("application payload is truncated (name %u)", index);
        }
        char *name = (char *)pyb_alloc((size_t)name_length + 1);
        memcpy(name, cursor, name_length);
        name[name_length] = '\0';
        cursor += name_length;
        if (!pyb_safe_name(name)) {
            pyb_die("refusing to extract unsafe path %s", name);
        }
        if ((size_t)(end - cursor) < compressed) {
            pyb_die("application payload is truncated (data %s)", name);
        }
        char *target = pyb_join2(destination, name);
        char *parent = pyb_dirname(target);
        if (pyb_mkdirs(parent) != 0) {
            pyb_die("cannot create directory %s", parent);
        }
        free(parent);
        if (kind == 1) {
            if (compressed == 0 || compressed > 4096) {
                pyb_die("invalid symlink entry %s", name);
            }
            char *link_target = (char *)pyb_alloc((size_t)compressed + 1);
            memcpy(link_target, cursor, compressed);
            link_target[compressed] = '\0';
            if (link_target[0] == '/' || strstr(link_target, "..") != NULL) {
                pyb_die("refusing to create unsafe symlink %s", name);
            }
#if defined(_WIN32)
            /* Windows runtimes do not contain symlinks; if one appears, the
             * target is extracted under its canonical name anyway. */
            (void)target;
#else
            unlink(target);
            if (symlink(link_target, target) != 0) {
                pyb_die("cannot create symlink %s -> %s", name, link_target);
            }
#endif
            free(link_target);
            cursor += compressed;
            free(target);
            free(name);
            continue;
        }
        FILE *output = pyb_fopen(target, "wb");
        if (output == NULL) {
            pyb_die("cannot write %s: %s", target, strerror(errno));
        }
        int status = 0;
        if (method == 0) {
            if (compressed != uncompressed ||
                (compressed > 0 && fwrite(cursor, 1, (size_t)compressed, output) !=
                                       (size_t)compressed)) {
                status = -1;
            }
        } else if (method == 8) {
            status = pyb_inflate_to_file(cursor, compressed, output, uncompressed);
        } else {
            status = -1;
        }
        if (fclose(output) != 0) {
            status = -1;
        }
        if (status != 0) {
            pyb_die("failed to extract %s (method %u)", name, method);
        }
        int file_mode = (int)(mode & 0777);
        if (file_mode == 0) {
            file_mode = 0644;
        }
        pyb_chmod(target, file_mode);
        cursor += compressed;
        free(target);
        free(name);
    }
    free(payload);
}

static int pyb_lock(const char *path, int timeout_seconds) {
    size_t length = strlen(path);
    char *suffix = (char *)pyb_alloc(length + 6);
    snprintf(suffix, length + 6, "%s.lock", path);

#if defined(_WIN32)
    wchar_t *wide_suffix = pyb_wide(suffix);
    if (wide_suffix == NULL) {
        free(suffix);
        return -1;
    }
    for (int attempt = 0; attempt < timeout_seconds * 10; attempt++) {
        HANDLE handle = CreateFileW(wide_suffix, GENERIC_WRITE, 0, NULL, CREATE_NEW,
                                    FILE_ATTRIBUTE_NORMAL, NULL);
        if (handle != INVALID_HANDLE_VALUE) {
            DWORD written = 0;
            WriteFile(handle, "lock", 4, &written, NULL);
            CloseHandle(handle);
            free(wide_suffix);
            free(suffix);
            return 0;
        }
        if (GetLastError() == ERROR_PATH_NOT_FOUND) {
            break;
        }
        if (attempt > 300) {
            DeleteFileW(wide_suffix);
        }
        Sleep(100);
    }
    free(wide_suffix);
#else
    for (int attempt = 0; attempt < timeout_seconds * 10; attempt++) {
        int descriptor = open(suffix, O_CREAT | O_EXCL | O_WRONLY, 0644);
        if (descriptor >= 0) {
            char text[32];
            int text_length = snprintf(text, sizeof(text), "%ld", (long)getpid());
            if (write(descriptor, text, (size_t)text_length) < 0) {
                /* the pid is advisory; a failed write is not fatal */
            }
            close(descriptor);
            free(suffix);
            return 0;
        }
        if (errno != EEXIST) {
            /* Missing parent directory or permission problem: retrying cannot
             * help, so surface the failure immediately. */
            break;
        }
        /* Remove locks left behind by a crashed process. */
        FILE *existing = fopen(suffix, "r");
        if (existing != NULL) {
            long pid = 0;
            if (fscanf(existing, "%ld", &pid) == 1 && pid > 0) {
                if (kill((pid_t)pid, 0) != 0 && errno == ESRCH) {
                    fclose(existing);
                    unlink(suffix);
                    continue;
                }
            }
            fclose(existing);
        }
        if (attempt > 300) {
            unlink(suffix);
            continue;
        }
        struct timespec pause;
        pause.tv_sec = 0;
        pause.tv_nsec = 100 * 1000 * 1000;
        nanosleep(&pause, NULL);
    }
#endif
    free(suffix);
    return -1;
}

static void pyb_unlock(const char *path) {
    size_t length = strlen(path);
    char *suffix = (char *)pyb_alloc(length + 6);
    snprintf(suffix, length + 6, "%s.lock", path);
#if defined(_WIN32)
    DeleteFileA(suffix);
#else
    unlink(suffix);
#endif
    free(suffix);
}

static char *pyb_onefile_root(const char *self, const char *base_dir) {
    uint64_t offset = 0;
    uint64_t length = 0;
    if (pyb_read_footer(self, &offset, &length) != 0) {
        pyb_die(
            "this executable is a onefile bundle but its payload footer is missing; "
            "the file was truncated or modified");
    }
    char *root = pyb_join3(base_dir, PYB_NAME, PYB_BUILD_ID);
    if (pyb_is_dir(root)) {
        return root;
    }
    if (pyb_mkdirs(base_dir) != 0) {
        pyb_die("cannot create cache directory %s", base_dir);
    }
    char *root_parent = pyb_dirname(root);
    if (pyb_mkdirs(root_parent) != 0) {
        pyb_die("cannot create cache directory %s", root_parent);
    }
    free(root_parent);
    if (pyb_lock(root, 120) != 0) {
        pyb_show_error("another instance is unpacking this application; giving up");
        free(root);
        exit(1);
    }
    if (pyb_is_dir(root)) {
        pyb_unlock(root);
        return root;
    }
    char suffix[64];
#if defined(_WIN32)
    snprintf(suffix, sizeof(suffix), ".tmp.%ld", (long)_getpid());
#else
    snprintf(suffix, sizeof(suffix), ".tmp.%ld", (long)getpid());
#endif
    char *staging = (char *)pyb_alloc(strlen(root) + strlen(suffix) + 1);
    sprintf(staging, "%s%s", root, suffix);
    pyb_remove_dir_tree(staging);
    pyb_extract(self, offset, length, staging);
    if (pyb_rename_dir(staging, root) != 0) {
        pyb_remove_dir_tree(staging);
        if (!pyb_is_dir(root)) {
            pyb_die("cannot move extracted application into %s", root);
        }
    }
    free(staging);
    pyb_unlock(root);
    return root;
}

/* ------------------------------------------------------------------ */
/* Environment and exec                                               */
/* ------------------------------------------------------------------ */

/*
 * When sys.executable points at the application (as in PyInstaller and in
 * PyBundler), the stdlib launches helper processes such as
 * ``multiprocessing.resource_tracker`` as ``app -B -s -P -c <code>``.  Those
 * must go straight to the bundled interpreter; only the application's own
 * arguments (and the special ``--multiprocessing-fork`` marker) are routed
 * through the bootstrap.
 */
#if !defined(_WIN32)
static int pyb_is_interpreter_invocation(int argc, char **argv) {
    for (int index = 1; index < argc; index++) {
        const char *argument = argv[index];
        if (strcmp(argument, "-c") == 0 || strcmp(argument, "-m") == 0) {
            return 1;
        }
        if (argument[0] != '-') {
            return 0;
        }
    }
    return 0;
}
#endif


static void pyb_prepend_env(const char *name, const char *value) {
    const char *existing = getenv(name);
    if (existing == NULL || existing[0] == '\0') {
        pyb_setenv(name, value);
        return;
    }
    char *combined = pyb_alloc(strlen(existing) + strlen(value) + 2);
#if defined(_WIN32)
    sprintf(combined, "%s;%s", value, existing);
#else
    sprintf(combined, "%s:%s", value, existing);
#endif
    pyb_setenv(name, combined);
    free(combined);
}

#if defined(_WIN32)

int main(void) {
#if PYB_WINDOWED
    FreeConsole();
#endif
    int argc = 0;
    LPWSTR *wide_argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    if (wide_argv == NULL) {
        pyb_die("cannot read the command line");
    }
    char *self = pyb_self_path(NULL);
    char *module_root = pyb_dirname(self);

#if PYB_MODE_ONEFILE
    char *cache = pyb_cache_dir();
    char *root = pyb_onefile_root(self, cache);
    free(cache);
    free(module_root);
    module_root = root;
#endif

    char *python = pyb_join2(module_root, PYB_EXE_REL);
    char *boot = pyb_join2(module_root, PYB_SITE_REL);
    char *boot_script = pyb_join2(boot, "_pybundler_boot.py");
    char *boot_bytecode = pyb_join2(boot, "_pybundler_boot.pyc");
    char *home = pyb_join2(module_root, PYB_HOME_REL);
    char *libs = pyb_join2(module_root, PYB_LIB_REL);

    if (!pyb_is_file(python)) {
        pyb_die("the bundle is incomplete: %s is missing", python);
    }
    if (!pyb_is_file(boot_script) && !pyb_is_file(boot_bytecode)) {
        pyb_die("the bundle is incomplete: %s is missing", boot_script);
    }

    pyb_setenv("PYTHONHOME", home);
    pyb_setenv("PYTHONPATH", boot);
    pyb_setenv("PYTHONNOUSERSITE", "1");
    pyb_setenv("PYTHONSAFEPATH", "1");
    pyb_setenv("PYTHONDONTWRITEBYTECODE", "1");
    pyb_setenv("PYTHONUTF8", "1");
    pyb_setenv("PYBUNDLER_ROOT", module_root);
    pyb_setenv("PYBUNDLER_APP_PATH", self);
    pyb_prepend_env("PATH", libs);
    pyb_prepend_env("PATH", pyb_dirname(python));

    int interpreter_invocation = 0;
    for (int index = 1; index < argc; index++) {
        if (wcscmp(wide_argv[index], L"-c") == 0 || wcscmp(wide_argv[index], L"-m") == 0) {
            interpreter_invocation = 1;
            break;
        }
        if (wide_argv[index][0] != L'-') {
            break;
        }
    }
    if (interpreter_invocation) {
        wchar_t *wide_python_passthrough = pyb_wide(python);
        wide_argv[0] = wide_python_passthrough;
        intptr_t passthrough_status =
            _wspawnv(_P_WAIT, wide_python_passthrough, (const wchar_t *const *)wide_argv);
        if (passthrough_status == -1) {
            pyb_die("cannot start the bundled interpreter: %s", strerror(errno));
        }
        return (int)passthrough_status;
    }

    wchar_t *wide_python = pyb_wide(python);
    wchar_t **arguments = (wchar_t **)pyb_alloc(sizeof(wchar_t *) * (size_t)(argc + 6));
    arguments[0] = wide_python;
    arguments[1] = L"-B";
    arguments[2] = L"-s";
    arguments[3] = L"-m";
    arguments[4] = L"_pybundler_boot";
    for (int index = 1; index < argc; index++) {
        arguments[index + 4] = wide_argv[index];
    }
    arguments[argc + 4] = NULL;

    intptr_t status = _wspawnv(_P_WAIT, wide_python, (const wchar_t *const *)arguments);
    if (status == -1) {
        pyb_die("cannot start the bundled interpreter: %s", strerror(errno));
    }
    return (int)status;
}

#else

int main(int argc, char **argv) {
    char *self = pyb_self_path(argc > 0 ? argv[0] : NULL);
    char *module_root = pyb_dirname(self);

#if PYB_MODE_ONEFILE
    char *cache = pyb_cache_dir();
    char *root = pyb_onefile_root(self, cache);
    free(cache);
    free(module_root);
    module_root = root;
#endif

    char *python = pyb_join2(module_root, PYB_EXE_REL);
    char *site = pyb_join2(module_root, PYB_SITE_REL);
    char *boot_script = pyb_join2(site, "_pybundler_boot.py");
    char *boot_bytecode = pyb_join2(site, "_pybundler_boot.pyc");
    char *home = pyb_join2(module_root, PYB_HOME_REL);
    char *libs = pyb_join2(module_root, PYB_LIB_REL);

    if (!pyb_is_file(python)) {
        pyb_die("the bundle is incomplete: %s is missing", python);
    }
    if (!pyb_is_file(boot_script) && !pyb_is_file(boot_bytecode)) {
        pyb_die("the bundle is incomplete: %s is missing", boot_script);
    }

    pyb_setenv("PYTHONHOME", home);
    pyb_setenv("PYTHONPATH", site);
    pyb_setenv("PYTHONNOUSERSITE", "1");
    pyb_setenv("PYTHONSAFEPATH", "1");
    pyb_setenv("PYTHONDONTWRITEBYTECODE", "1");
    pyb_setenv("PYTHONUTF8", "1");
    pyb_setenv("PYBUNDLER_ROOT", module_root);
    pyb_setenv("PYBUNDLER_APP_PATH", self);
    pyb_prepend_env("DYLD_LIBRARY_PATH", libs);
    pyb_prepend_env("LD_LIBRARY_PATH", libs);
    pyb_prepend_env("PATH", pyb_dirname(python));

    if (getenv("PYBUNDLER_DEBUG") != NULL) {
        fprintf(stderr, "[pybundler] root=%s\n", module_root);
        fprintf(stderr, "[pybundler] python=%s\n", python);
        fprintf(stderr, "[pybundler] boot=%s\n", boot_script);
    }

    if (pyb_is_interpreter_invocation(argc, argv)) {
        argv[0] = python;
        execv(python, argv);
        pyb_die("cannot start the bundled interpreter %s: %s", python, strerror(errno));
    }

    char **arguments = (char **)pyb_alloc(sizeof(char *) * (size_t)(argc + 6));
    arguments[0] = python;
    arguments[1] = (char *)"-B";
    arguments[2] = (char *)"-s";
    arguments[3] = (char *)"-m";
    arguments[4] = (char *)"_pybundler_boot";
    for (int index = 1; index < argc; index++) {
        arguments[index + 4] = argv[index];
    }
    arguments[argc + 4] = NULL;

    execv(python, arguments);
    pyb_die("cannot start the bundled interpreter %s: %s", python, strerror(errno));
    return 127;
}

#endif
