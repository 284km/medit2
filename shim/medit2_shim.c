/* shim/medit2_shim.c — the two things Mere's host does not have.
 *
 * Both are `extern fn` declarations on the Mere side and plain C here: the
 * compiler emits an `extern` prototype for any name it does not implement
 * itself, so this links with no change to mere at all. That is the same door
 * contrib/window went through, except that one ended up inside the compiler
 * and this one has no reason to.
 *
 * Build:
 *   mere -c medit2.mere > medit2.c
 *   clang -O2 medit2.c shim/medit2_shim.c -o medit2
 */

#include <fcntl.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <signal.h>
#include <unistd.h>

/* --- the terminal's size -------------------------------------------------
 * -1 when stdout is not a terminal, which is a real answer: the editor runs
 * piped in its own tests and has to notice. No SIGWINCH handler: the event
 * loop returns from io_poll_wait many times a second and can just ask again,
 * which costs a syscall and avoids a signal handler racing the redraw.
 */
int term_rows(void) {
  struct winsize w;
  if (ioctl(1, TIOCGWINSZ, &w) != 0) return -1;
  return w.ws_row;
}

int term_cols(void) {
  struct winsize w;
  if (ioctl(1, TIOCGWINSZ, &w) != 0) return -1;
  return w.ws_col;
}

/* --- a long-lived child, on one bidirectional fd -------------------------
 * A socketpair rather than two pipes, and that is the whole trick: the child
 * ends up looking EXACTLY like an accepted TCP connection, so Mere's existing
 * tcp_read / tcp_write / tcp_close / io_poll_add all work on it unchanged.
 * tcp_read and tcp_write are read(2) and write(2), not recv/send, so nothing
 * about them is socket-specific.
 *
 * Returns the parent's end, or -1. The caller owns it and closes it with
 * tcp_close.
 *
 * SIGPIPE is ignored once, here, because a language server that exits leaves
 * the editor writing into a dead socket -- and the default disposition kills
 * the editor, losing the buffer. With it ignored, tcp_write returns -1 and the
 * editor can say so.
 */
int proc_open(const char* cmd) {
  static int sigpipe_done = 0;
  if (!sigpipe_done) { signal(SIGPIPE, SIG_IGN); sigpipe_done = 1; }

  int sv[2];
  if (socketpair(AF_UNIX, SOCK_STREAM, 0, sv) != 0) return -1;

  pid_t pid = fork();
  if (pid < 0) { close(sv[0]); close(sv[1]); return -1; }
  if (pid == 0) {
    close(sv[0]);
    dup2(sv[1], 0);
    dup2(sv[1], 1);
    /* stderr is left alone deliberately: a language server's diagnostics go to
     * the terminal, where they would corrupt the screen. Send it to /dev/null
     * so a chatty server cannot scribble over the editor. */
    int devnull = open("/dev/null", O_WRONLY);
    if (devnull >= 0) { dup2(devnull, 2); close(devnull); }
    close(sv[1]);
    execl("/bin/sh", "sh", "-c", cmd, (char*)0);
    _exit(127);
  }
  close(sv[1]);
  return sv[0];
}

/* Reap whatever has exited, so a server that dies does not become a zombie.
 * Non-blocking: the editor calls this on its own schedule rather than being
 * interrupted by SIGCHLD mid-redraw. Returns how many were reaped. */
int proc_reap(void) {
  int n = 0;
  while (waitpid(-1, 0, WNOHANG) > 0) n++;
  return n;
}

/* --- what tty_raw still leaves to the program ---------------------------
 * Two input/output translations, and they are the editor's own choice rather
 * than something raw mode should decide:
 *
 *   ICRNL  with it on, Enter arrives as 10 and is indistinguishable from a
 *          pasted newline. Cleared, Enter is 13 and the editor can tell them
 *          apart -- which matters, because one starts a line and the other is
 *          content.
 *   OPOST  with it on the terminal turns every \n into \r\n. The editor emits
 *          its own \r\n, so leaving it on doubles the carriage return.
 *
 * The flow-control and signal-key halves used to be here too. They are not any
 * more: `tty_raw` clears IXON/IXOFF as of mere v0.1.476 (Ctrl-S was XOFF, so
 * the save key froze the terminal), and `tty_no_signal_keys` clears ISIG for a
 * program that wants Ctrl-Z as a byte rather than as SUSP. Both went upstream
 * because they are not this editor's private problem -- the medit dogfood had
 * the same bug and could not be saved or quit from at all.
 */
#include <termios.h>

int term_editor_output(void) {
  struct termios t;
  if (tcgetattr(0, &t) != 0) return -1;   /* not a tty: piped input, fine */
  t.c_iflag &= ~ICRNL;
  t.c_oflag &= ~OPOST;
  return tcsetattr(0, TCSANOW, &t) == 0 ? 0 : -1;
}
