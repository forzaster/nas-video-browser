// LaunchAgent entry point bundled as FamilyVideos.app.
// macOS attributes Local Network access to the "responsible" process; children started here (bash, uv, python)
// stay attributed to this app, so the permission shows up as FamilyVideos instead of uv.
// It runs argv[1..] as a child (not exec, which would hand responsibility to the next binary), forwards
// stop signals to it, and exits with its status.
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;
static volatile pid_t child = 0;

static void forward(int sig) {
    if (child > 0) kill(child, sig);
}

int main(int argc, char *argv[]) {
    if (argc < 2) {
        fprintf(stderr, "usage: FamilyVideos COMMAND [ARGS...]\n");
        return 64;
    }
    struct sigaction action;
    memset(&action, 0, sizeof action);
    action.sa_handler = forward;
    sigemptyset(&action.sa_mask);
    int signals[] = {SIGTERM, SIGINT, SIGHUP, SIGQUIT};
    for (size_t i = 0; i < sizeof signals / sizeof *signals; i++) sigaction(signals[i], &action, NULL);

    pid_t pid;
    int error = posix_spawn(&pid, argv[1], NULL, NULL, &argv[1], environ);
    if (error) {
        fprintf(stderr, "FamilyVideos: cannot start %s: %s\n", argv[1], strerror(error));
        return 127;
    }
    child = pid;
    int status;
    while (waitpid(pid, &status, 0) < 0) {
        // interrupted by a forwarded signal: keep waiting for the child to exit
    }
    if (WIFEXITED(status)) return WEXITSTATUS(status);
    if (WIFSIGNALED(status)) return 128 + WTERMSIG(status);
    return 1;
}
