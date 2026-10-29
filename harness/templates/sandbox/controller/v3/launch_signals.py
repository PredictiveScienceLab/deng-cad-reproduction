"""Route ordinary controller termination through Python cleanup blocks."""
import signal

def install_exit_signals():
    stopping=[False]
    def stop(number,frame):
        # A second signal must not interrupt worker/CLI cleanup in finally.
        if stopping[0]:return
        stopping[0]=True
        raise SystemExit(128+number)
    for number in (signal.SIGTERM,signal.SIGINT):signal.signal(number,stop)
    return stopping
