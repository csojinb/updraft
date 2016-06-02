"""
    This module is adapted from Werkzeug's tests.conftest module.

    :copyright: (c) 2014 by the Werkzeug Team, see COPYRIGHT-NOTICE for more
    details.
    :license: BSD, see COPYRIGHT-NOTICE for more details.
"""

import os
import textwrap
import time
import signal
import sys

import pytest
import requests

from updraft import serving
from updraft.middleware import BasicMiddleware
from updraft._compat import to_bytes


@pytest.fixture
def subprocess(xprocess):
    # Note: this fixture is provided by the pytest-xprocess plugin
    return xprocess


def _patch_reloader_loop():
    def f(x):
        print('reloader loop finished')
        return time.sleep(x)

    import updraft._reloader
    updraft._reloader.ReloaderLoop._sleep = staticmethod(f)


class PIDMiddleware(BasicMiddleware):

    def __call__(self, environ, start_response):
        if environ['PATH_INFO'] == '/_getpid':
            start_response('200 OK', [('Content-Type', 'text/plain')])
            return [to_bytes(str(os.getpid()))]
        return self.app(environ, start_response)


def _get_pid_middleware(app):
    return PIDMiddleware(app)


def _dev_server():
    # import pdb; pdb.set_trace()
    _patch_reloader_loop()
    sys.path.insert(0, sys.argv[1])
    import testsuite_app
    app = _get_pid_middleware(testsuite_app.app)
    serving.run_simple(hostname='localhost', application=app,
                       **testsuite_app.kwargs)

if __name__ == '__main__':
    _dev_server()


class _ServerInfo(object):

    def __init__(self, xprocess, addr, url, port):
        self.xprocess = xprocess
        self.addr = addr
        self.url = url
        self.port = port
        self.last_pid = None
        self._logfile = None

    @property
    def logfile(self):
        if self._logfile is None:
            self._logfile = self.xprocess.getinfo('dev_server').logpath.open()

        return self._logfile

    def request_pid(self):
        for i in range(20):
            time.sleep(0.1 * i)
            try:
                self.last_pid = int(requests.get(self.url + '/_getpid',
                                                 verify=False).text)
                return self.last_pid
            except Exception as e:  # urllib also raises socketerrors
                pass
        return False


@pytest.fixture
def test_server(tmpdir, subprocess, request, monkeypatch):
    """
    Run a dev server in a separate process for testing.

    :param application: String with contents of module that will be created.
    Module must have a global `app` object. It may optionally have a global
    `kwargs` dict that specifies parameters to pass into the test server.
    """

    def run_test_server(application):
        app_pkg = tmpdir.mkdir('testsuite_app')
        appfile = app_pkg.join('__init__.py')
        appfile.write('\n\n'.join((
            'import falcon',
            'kwargs = dict(port=5001)',
            'app = falcon.API()',
            textwrap.dedent(application),
            "app.add_route('/resource', Resource())"
        )))

        monkeypatch.delitem(sys.modules, 'testsuite_app', raising=False)
        monkeypatch.syspath_prepend(str(tmpdir))
        import testsuite_app
        port = testsuite_app.kwargs['port']

        url_base = 'http://localhost:{}'.format(port)

        info = _ServerInfo(
            subprocess,
            'localhost:{}'.format(port),
            url_base,
            port
        )

        def preparefunc(cwd):
            args = [sys.executable, __file__, str(tmpdir)]
            return info.request_pid, args

        subprocess.ensure('test_server', preparefunc, restart=True)

        def teardown():
            # Killing the process group that runs the server, not just the
            # parent process attached. xprocess is confused about Werkzeug's
            # reloader and won't help here.
            pid = info.last_pid
            os.killpg(os.getpgid(pid), signal.SIGTERM)

        request.addfinalizer(teardown)

        return info

    return run_test_server
