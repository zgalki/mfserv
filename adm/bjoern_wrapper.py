#!/usr/bin/env python

import argparse
import sys
import bjoern
import importlib
import signal
import datetime
import functools
import base64
import time
import os
import requests
import threading
import mflog
import mfutil


LOGGER = mflog.get_logger("bjoern_wrapper")
MFSERV_NGINX_PORT = int(os.environ['MFSERV_NGINX_PORT'])
MODULE = os.environ['MODULE']
CURRENT_PLUGIN_NAME_ENV_VAR = "%s_CURRENT_PLUGIN_NAME" % MODULE
if CURRENT_PLUGIN_NAME_ENV_VAR in os.environ:
    PLUGIN = os.environ[CURRENT_PLUGIN_NAME_ENV_VAR]
else:
    PLUGIN = None


def unix_socket_encode(unix_socket):
    return base64.urlsafe_b64encode(unix_socket.encode('utf8')).decode('ascii')


def send_sigint(pid):
    LOGGER.debug("sending SIGINT to %i", pid)
    os.kill(pid, signal.SIGINT)


def get_socket_conns(unix_socket):
    url = "http://127.0.0.1:%i/__upstream_status" % MFSERV_NGINX_PORT
    try:
        res = requests.get(url, timeout=10).json()
        for peers in res.values():
            for peer in peers:
                if peer.get('name', None) == 'unix:' + unix_socket:
                    return peer.get('conns', None)
    except Exception:
        return None


def send_sigint_when_no_connection(pid, unix_socket, timeout):
    try:
        params = {"wait": "1"}
        requests.get(
            "http://127.0.0.1:%i/__socket_down/%s" % (
                MFSERV_NGINX_PORT, unix_socket_encode(unix_socket)),
            params=params)
    except Exception:
        pass
    send_sigint(os.getpid())


def on_signal(unix_socket, timeout, signum, frame):
    if signum == 15:
        LOGGER.debug("received SIGTERM => smart closing...")
        x = threading.Thread(target=send_sigint_when_no_connection,
                             args=(os.getpid(), unix_socket, timeout))
        x.daemon = True
        x.start()


def socket_up_after(unix_socket, after):
    time.sleep(after)
    LOGGER.debug("socket_up on nginx")
    requests.get("http://127.0.0.1:%i/__socket_up/%s" % (
        MFSERV_NGINX_PORT, unix_socket_encode(unix_socket)), timeout=10)


def get_wsgi_application(path):
    if len(path.split(':')) != 2:
        LOGGER.warning("main_arg must follow module.submodule:func_name")
        sys.exit(1)
    module_path, func_name = path.split(':')
    mod = importlib.import_module(module_path)
    try:
        return getattr(mod, func_name)
    except Exception:
        LOGGER.warning("can't find: %s func_name in module: %s" % (
            func_name, mod))
        sys.exit(1)


class TimeoutWsgiMiddlewareException(Exception):

    pass


class TimeoutWsgiMiddleware(object):

    def __init__(self, app, timeout, hard_timeout=None):
        self.app = app
        self.timeout = timeout
        if hard_timeout is None:
            self.hard_timeout = timeout + 1
        else:
            self.hard_timeout = hard_timeout
        self.started = None
        if self.hard_timeout > 0:
            x = threading.Thread(target=self.hard_timeout_handler)
            x.daemon = True
            x.start()

    def hard_timeout_handler(self):
        now = datetime.datetime.now
        while True:
            if self.started:
                if (now() - self.started).total_seconds() > self.hard_timeout:
                    LOGGER.warning("Request (hard) Timeout => SIGKILL")
                    # Self-Kill
                    mfutil.kill_process_and_children(os.getpid())
            time.sleep(1)

    def signal_timeout_handler(self, signum, frame):
        LOGGER.warning("Request (soft) Timeout => HTTP/504")
        raise TimeoutWsgiMiddlewareException("soft timeout")

    def __call__(self, environ, start_response):
        if self.hard_timeout > 0:
            self.started = datetime.datetime.now()
        iterable = None
        if self.timeout > 0:
            signal.signal(signal.SIGALRM, self.signal_timeout_handler)
            signal.alarm(self.timeout)
        soft_timeout_exc_info = None
        try:
            # see http://blog.dscpl.com.au/2012/10/
            #     obligations-for-calling-close-on.html
            iterable = self.app(environ, start_response)
            for data in iterable:
                yield data
        except TimeoutWsgiMiddlewareException:
            soft_timeout_exc_info = sys.exc_info()
        finally:
            self.started = None
            if self.timeout > 0:
                signal.alarm(0)
            if hasattr(iterable, 'close'):
                iterable.close()
        if soft_timeout_exc_info:
            response_headers = [('Content-Type', 'text/plain')]
            start_response("504 Gateway Time-out", response_headers,
                           soft_timeout_exc_info)
            return


class MflogWsgiMiddleware(object):

    def __init__(self, app, raise_exception=False, debug=False):
        self.app = app
        self.raise_exception = raise_exception
        self.debug = debug
        if self.debug:
            mflog.set_config("DEBUG")

    def __call__(self, environ, start_response):
        if "HTTP_X_REQUEST_ID" in environ:
            request_id = environ["HTTP_X_REQUEST_ID"]
            os.environ["MFSERV_CURRENT_REQUEST_ID"] = request_id
        iterable = None
        try:
            # see http://blog.dscpl.com.au/2012/10/
            #     obligations-for-calling-close-on.html
            iterable = self.app(environ, start_response)
            for data in iterable:
                yield data
            if hasattr(iterable, 'close'):
                iterable.close()
        except Exception:
            if hasattr(iterable, 'close'):
                iterable.close()
            LOGGER.exception("uncatched exception")
            if self.raise_exception:
                raise
            output = b"HTTP/500 Internal Server Error"
            response_headers = [('Content-Type', 'text/plain'),
                                ('Content-Length', str(len(output)))]
            start_response("500 Internal Server Error", response_headers,
                           sys.exc_info())
            yield output


def main():
    parser = argparse.ArgumentParser(description="bjoern wrapper")
    parser.add_argument("main_arg", help="wsgi application path")
    parser.add_argument("unix_socket", help="unix socket to listen path")
    parser.add_argument("--timeout", default=60, type=int,
                        help="one request execution timeout (in seconds)")
    parser.add_argument("--debug", action="store_true",
                        help="if set, debug exceptions in browser (do not use "
                        "in production!)")
    parser.add_argument("--debug-evalex", action="store_true",
                        help="if set, you can interactively debug your app in "
                        "your brower (never use it in production!)")
    args = parser.parse_args()
    signal.signal(signal.SIGTERM,
                  functools.partial(on_signal, args.unix_socket, args.timeout))
    wsgi_app = get_wsgi_application(args.main_arg)
    try:
        os.unlink(args.unix_socket)
    except Exception:
        pass
    x = threading.Thread(target=socket_up_after, args=(args.unix_socket, 1))
    x.daemon = True
    x.start()
    try:
        app = MflogWsgiMiddleware(
            TimeoutWsgiMiddleware(wsgi_app, args.timeout), args.debug,
            args.debug)
        if args.debug:
            try:
                from werkzeug.debug import DebuggedApplication
                app = DebuggedApplication(app, evalex=args.debug_evalex,
                                          pin_security=False)
            except ImportError:
                LOGGER.warning(
                    "can't import werkzeug, maybe you need to "
                    "install metwork-mfext-layer-python%i_devtools "
                    "package ?", int(os.environ['METWORK_PYTHON_MODE']))
        LOGGER.debug("process start")
        bjoern.run(app, 'unix:%s' % args.unix_socket, listen_backlog=10000)
    except KeyboardInterrupt:
        LOGGER.debug("process (normal) shutdown")
    except Exception:
        LOGGER.exception("uncatched exception")
    try:
        os.remove(args.unix_socket)
    except Exception:
        pass


if __name__ == '__main__':
    main()
