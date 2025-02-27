"""Job control for the xonsh shell."""
import os
import sys
import time
import ctypes
import signal
import subprocess
import collections
import typing as tp

from xonsh.built_ins import XSH
from xonsh.cli_utils import Annotated, Arg, ArgParserAlias
from xonsh.completers.tools import RichCompletion
from xonsh.lazyasd import LazyObject
from xonsh.platform import FD_STDERR, ON_DARWIN, ON_WINDOWS, ON_CYGWIN, ON_MSYS, LIBC
from xonsh.tools import unthreadable

# there is not much cost initing deque
tasks: tp.Deque[int] = collections.deque()
# Track time stamp of last exit command, so that two consecutive attempts to
# exit can kill all jobs and exit.
_last_exit_time: tp.Optional[float] = None


if ON_DARWIN:

    def _send_signal(job, signal):
        # On OS X, os.killpg() may cause PermissionError when there are
        # any zombie processes in the process group.
        # See github issue #1012 for details
        for pid in job["pids"]:
            if pid is None:  # the pid of an aliased proc is None
                continue
            try:
                os.kill(pid, signal)
            except ProcessLookupError:
                pass

elif ON_WINDOWS:
    pass
elif ON_CYGWIN or ON_MSYS:
    # Similar to what happened on OSX, more issues on Cygwin
    # (see Github issue #514).
    def _send_signal(job, signal):
        try:
            os.killpg(job["pgrp"], signal)
        except Exception:
            for pid in job["pids"]:
                try:
                    os.kill(pid, signal)
                except Exception:
                    pass

else:

    def _send_signal(job, signal):
        pgrp = job["pgrp"]
        if pgrp is None:
            for pid in job["pids"]:
                try:
                    os.kill(pid, signal)
                except Exception:
                    pass
        else:
            os.killpg(job["pgrp"], signal)


if ON_WINDOWS:

    def _continue(job):
        job["status"] = "running"

    def _kill(job):
        subprocess.check_output(
            ["taskkill", "/F", "/T", "/PID", str(job["obj"].pid)],
            stderr=subprocess.STDOUT,
        )

    def ignore_sigtstp():
        pass

    def give_terminal_to(pgid):
        pass

    def wait_for_active_job(last_task=None, backgrounded=False, return_error=False):
        """
        Wait for the active job to finish, to be killed by SIGINT, or to be
        suspended by ctrl-z.
        """
        active_task = get_next_task()
        # Return when there are no foreground active task
        if active_task is None:
            return last_task
        obj = active_task["obj"]
        _continue(active_task)
        while obj.returncode is None:
            try:
                obj.wait(0.01)
            except subprocess.TimeoutExpired:
                pass
            except KeyboardInterrupt:
                try:
                    _kill(active_task)
                except subprocess.CalledProcessError:
                    pass  # ignore error if process closed before we got here
        return wait_for_active_job(last_task=active_task)

else:

    def _continue(job):
        _send_signal(job, signal.SIGCONT)
        job["status"] = "running"

    def _kill(job):
        _send_signal(job, signal.SIGKILL)

    def ignore_sigtstp():
        signal.signal(signal.SIGTSTP, signal.SIG_IGN)

    _shell_pgrp = os.getpgrp()  # type:ignore

    _block_when_giving = LazyObject(
        lambda: (
            signal.SIGTTOU,  # type:ignore
            signal.SIGTTIN,  # type:ignore
            signal.SIGTSTP,  # type:ignore
            signal.SIGCHLD,  # type:ignore
        ),
        globals(),
        "_block_when_giving",
    )

    if ON_CYGWIN or ON_MSYS:
        # on cygwin, signal.pthread_sigmask does not exist in Python, even
        # though pthread_sigmask is defined in the kernel.  thus, we use
        # ctypes to mimic the calls in the "normal" version below.
        LIBC.pthread_sigmask.restype = ctypes.c_int
        LIBC.pthread_sigmask.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong),
        ]

        def _pthread_sigmask(how, signals):
            mask = 0
            for sig in signals:
                mask |= 1 << sig
            oldmask = ctypes.c_ulong()
            mask = ctypes.c_ulong(mask)
            result = LIBC.pthread_sigmask(
                how, ctypes.byref(mask), ctypes.byref(oldmask)
            )
            if result:
                raise OSError(result, "Sigmask error.")

            return {
                sig
                for sig in getattr(signal, "Signals", range(0, 65))
                if (oldmask.value >> sig) & 1
            }

    else:
        _pthread_sigmask = signal.pthread_sigmask  # type:ignore

    # give_terminal_to is a simplified version of:
    #    give_terminal_to from bash 4.3 source, jobs.c, line 4030
    # this will give the terminal to the process group pgid
    def give_terminal_to(pgid):
        if pgid is None:
            return False
        oldmask = _pthread_sigmask(signal.SIG_BLOCK, _block_when_giving)
        try:
            os.tcsetpgrp(FD_STDERR, pgid)
            return True
        except ProcessLookupError:
            # when the process finished before giving terminal to it,
            # see issue #2288
            return False
        except OSError as e:
            if e.errno == 22:  # [Errno 22] Invalid argument
                # there are cases that all the processes of pgid have
                # finished, then we don't need to do anything here, see
                # issue #2220
                return False
            elif e.errno == 25:  # [Errno 25] Inappropriate ioctl for device
                # There are also cases where we are not connected to a
                # real TTY, even though we may be run in interactive
                # mode. See issue #2267 for an example with emacs
                return False
            else:
                raise
        finally:
            _pthread_sigmask(signal.SIG_SETMASK, oldmask)

    def wait_for_active_job(last_task=None, backgrounded=False, return_error=False):
        """
        Wait for the active job to finish, to be killed by SIGINT, or to be
        suspended by ctrl-z.
        """
        active_task = get_next_task()
        # Return when there are no foreground active task
        if active_task is None:
            return last_task
        obj = active_task["obj"]
        backgrounded = False
        try:
            _, wcode = os.waitpid(obj.pid, os.WUNTRACED)
        except ChildProcessError as e:  # No child processes
            if return_error:
                return e
            else:
                return _safe_wait_for_active_job(
                    last_task=active_task, backgrounded=backgrounded
                )
        if os.WIFSTOPPED(wcode):
            active_task["status"] = "stopped"
            backgrounded = True
        elif os.WIFSIGNALED(wcode):
            print()  # get a newline because ^C will have been printed
            obj.signal = (os.WTERMSIG(wcode), os.WCOREDUMP(wcode))
            obj.returncode = None
        else:
            obj.returncode = os.WEXITSTATUS(wcode)
            obj.signal = None
        return wait_for_active_job(last_task=active_task, backgrounded=backgrounded)


def _safe_wait_for_active_job(last_task=None, backgrounded=False):
    """Safely call wait_for_active_job()"""
    have_error = True
    while have_error:
        try:
            rtn = wait_for_active_job(
                last_task=last_task, backgrounded=backgrounded, return_error=True
            )
        except ChildProcessError as e:
            rtn = e
        have_error = isinstance(rtn, ChildProcessError)
    return rtn


def get_next_task():
    """Get the next active task and put it on top of the queue"""
    _clear_dead_jobs()
    selected_task = None
    for tid in tasks:
        task = get_task(tid)
        if not task["bg"] and task["status"] == "running":
            selected_task = tid
            break
    if selected_task is None:
        return
    tasks.remove(selected_task)
    tasks.appendleft(selected_task)
    return get_task(selected_task)


def get_task(tid):
    return XSH.all_jobs[tid]


def _clear_dead_jobs():
    to_remove = set()
    for tid in tasks:
        obj = get_task(tid)["obj"]
        if obj is None or obj.poll() is not None:
            to_remove.add(tid)
    for job in to_remove:
        tasks.remove(job)
        del XSH.all_jobs[job]


def format_job_string(num: int) -> str:
    try:
        job = XSH.all_jobs[num]
    except KeyError:
        return ""
    pos = "+" if tasks[0] == num else "-" if tasks[1] == num else " "
    status = job["status"]
    cmd = " ".join([" ".join(i) if isinstance(i, list) else i for i in job["cmds"]])
    pid = job["pids"][-1]
    bg = " &" if job["bg"] else ""
    return f"[{num}]{pos} {status}: {cmd}{bg} ({pid})"


def print_one_job(num, outfile=sys.stdout):
    """Print a line describing job number ``num``."""
    info = format_job_string(num)
    if info:
        print(info, file=outfile)


def get_next_job_number():
    """Get the lowest available unique job number (for the next job created)."""
    _clear_dead_jobs()
    i = 1
    while i in XSH.all_jobs:
        i += 1
    return i


def add_job(info):
    """Add a new job to the jobs dictionary."""
    num = get_next_job_number()
    info["started"] = time.time()
    info["status"] = "running"
    tasks.appendleft(num)
    XSH.all_jobs[num] = info
    if info["bg"] and XSH.env.get("XONSH_INTERACTIVE"):
        print_one_job(num)


def clean_jobs():
    """Clean up jobs for exiting shell

    In non-interactive mode, kill all jobs.

    In interactive mode, check for suspended or background jobs, print a
    warning if any exist, and return False. Otherwise, return True.
    """
    jobs_clean = True
    if XSH.env["XONSH_INTERACTIVE"]:
        _clear_dead_jobs()

        if XSH.all_jobs:
            global _last_exit_time
            hist = XSH.history
            if hist is not None and len(hist.tss) > 0:
                last_cmd_start = hist.tss[-1][0]
            else:
                last_cmd_start = None

            if _last_exit_time and last_cmd_start and _last_exit_time > last_cmd_start:
                # Exit occurred after last command started, so it was called as
                # part of the last command and is now being called again
                # immediately. Kill jobs and exit without reminder about
                # unfinished jobs in this case.
                kill_all_jobs()
            else:
                if len(XSH.all_jobs) > 1:
                    msg = "there are unfinished jobs"
                else:
                    msg = "there is an unfinished job"

                if XSH.env["SHELL_TYPE"] != "prompt_toolkit":
                    # The Ctrl+D binding for prompt_toolkit already inserts a
                    # newline
                    print()
                print(f"xonsh: {msg}", file=sys.stderr)
                print("-" * 5, file=sys.stderr)
                jobs([], stdout=sys.stderr)
                print("-" * 5, file=sys.stderr)
                print(
                    'Type "exit" or press "ctrl-d" again to force quit.',
                    file=sys.stderr,
                )
                jobs_clean = False
                _last_exit_time = time.time()
    else:
        kill_all_jobs()

    return jobs_clean


def kill_all_jobs():
    """
    Send SIGKILL to all child processes (called when exiting xonsh).
    """
    _clear_dead_jobs()
    for job in XSH.all_jobs.values():
        _kill(job)


def jobs(args, stdin=None, stdout=sys.stdout, stderr=None):
    """
    xonsh command: jobs

    Display a list of all current jobs.
    """
    _clear_dead_jobs()
    for j in tasks:
        print_one_job(j, outfile=stdout)
    return None, None


def resume_job(args, wording):
    """
    used by fg and bg to resume a job either in the foreground or in the background.
    """
    _clear_dead_jobs()
    if len(tasks) == 0:
        return "", "There are currently no suspended jobs"

    if len(args) == 0:
        tid = tasks[0]  # take the last manipulated task by default
    elif len(args) == 1:
        try:
            if args[0] == "+":  # take the last manipulated task
                tid = tasks[0]
            elif args[0] == "-":  # take the second to last manipulated task
                tid = tasks[1]
            else:
                tid = int(args[0])
        except (ValueError, IndexError):
            return "", f"Invalid job: {args[0]}\n"

        if tid not in XSH.all_jobs:
            return "", f"Invalid job: {args[0]}\n"
    else:
        return "", f"{wording} expects 0 or 1 arguments, not {len(args)}\n"

    # Put this one on top of the queue
    tasks.remove(tid)
    tasks.appendleft(tid)

    job = get_task(tid)
    job["bg"] = False
    job["status"] = "running"
    if XSH.env.get("XONSH_INTERACTIVE"):
        print_one_job(tid)
    pipeline = job["pipeline"]
    pipeline.resume(job)


@unthreadable
def fg(args, stdin=None):
    """
    xonsh command: fg

    Bring the currently active job to the foreground, or, if a single number is
    given as an argument, bring that job to the foreground. Additionally,
    specify "+" for the most recent job and "-" for the second most recent job.
    """
    return resume_job(args, wording="fg")


def bg(args, stdin=None):
    """xonsh command: bg

    Resume execution of the currently active job in the background, or, if a
    single number is given as an argument, resume that job in the background.
    """
    res = resume_job(args, wording="bg")
    if res is None:
        curtask = get_task(tasks[0])
        curtask["bg"] = True
        _continue(curtask)
    else:
        return res


def job_id_completer(xsh, **_):
    """Return currently running jobs ids"""
    for job_id in xsh.all_jobs:
        yield RichCompletion(str(job_id), description=format_job_string(job_id))


def disown_fn(
    job_ids: Annotated[
        tp.Sequence[int], Arg(type=int, nargs="*", completer=job_id_completer)
    ],
    force_auto_continue=False,
):
    """Remove the specified jobs from the job table; the shell will no longer
    report their status, and will not complain if you try to exit an
    interactive shell with them running or stopped.

    If the jobs are currently stopped and the $AUTO_CONTINUE option is not set
    ($AUTO_CONTINUE = False), a warning is printed containing information about
    how to make them continue after they have been disowned.

    Parameters
    ----------
    job_ids
        Jobs to act on or none to disown the current job
    force_auto_continue : -c, --continue
        Automatically continue stopped jobs when they are disowned, equivalent to setting $AUTO_CONTINUE=True
    """

    if len(tasks) == 0:
        return "", "There are no active jobs"

    messages = []
    # if args.job_ids is empty, use the active task
    for tid in job_ids or [tasks[0]]:
        try:
            current_task = get_task(tid)
        except KeyError:
            return "", f"'{tid}' is not a valid job ID"

        auto_cont = XSH.env.get("AUTO_CONTINUE", False)
        if auto_cont or force_auto_continue:
            _continue(current_task)
        elif current_task["status"] == "stopped":
            messages.append(
                f"warning: job is suspended, use "
                f"'kill -CONT -{current_task['pids'][-1]}' "
                f"to resume\n"
            )

        # Stop tracking this task
        tasks.remove(tid)
        del XSH.all_jobs[tid]
        messages.append(f"Removed job {tid} ({current_task['status']})")

    if messages:
        return "".join(messages)


disown = ArgParserAlias(prog="disown", func=disown_fn, has_args=True)
