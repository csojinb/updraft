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


class PIDMiddleware(BasicMiddleware):

    def __call__(self, environ, start_response):
        if environ['PATH_INFO'] == '/_getpid':
            start_response('200 OK', [('Content-Type', 'text/plain')])
            return [to_bytes(str(os.getpid()))]
        return self.app(environ, start_response)


def _get_pid_middleware(app):
    return PIDMiddleware(app)


def _dev_server():
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
            self._logfile = self.xprocess.getinfo('test_server').logpath.open()

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

    class TestServer(object):
        subprocess_name = 'test_server'
        last_pid = None

        def __init__(self, application):
            self.app_pkg = tmpdir.mkdir('testsuite_app')
            self.appfile = self.app_pkg.join('__init__.py')
            self._write_app_to_file(application)
            self._build_server_info()
            self._initialize_logfile()

        def overwrite_application(self, application):
            self.appfile.truncate()
            self._write_app_to_file(application)

        def request_pid(self):
            for i in range(20):
                time.sleep(0.1 * i)
                try:
                    self.last_pid = int(requests.get(self.url + '/_getpid',
                                                     verify=False).text)
                    return self.last_pid
                except Exception:
                    pass
            return False

        def run(self, subprocess):
            subprocess.ensure(
                self.subprocess_name, self.preparefunc, restart=True)

        def teardown(self):
            os.killpg(os.getpgid(self.last_pid), signal.SIGTERM)

        def preparefunc(self, cwd):
            args = [sys.executable, __file__, str(tmpdir)]
            return self.request_pid, args

        def _write_app_to_file(self, application):
            self.appfile.write('\n\n'.join((
                'import falcon',
                'kwargs = dict(port=5001)',
                'app = falcon.API()',
                textwrap.dedent(application),
                "app.add_route('/resource', Resource())"
            )))

        def _build_server_info(self):
            testsuite_app = self._load_app_as_package()
            self.port = testsuite_app.kwargs['port']
            self.addr = 'localhost:{}'.format(self.port)
            self.url = 'http://{}'.format(self.addr)

        def _initialize_logfile(self):
            self.logfile = subprocess.getinfo(self.subprocess_name).logpath.open()

        def _load_app_as_package(self):
            monkeypatch.delitem(sys.modules, 'testsuite_app', raising=False)
            monkeypatch.syspath_prepend(str(tmpdir))
            import testsuite_app
            return testsuite_app

    def run_test_server(application):
        server = TestServer(application)
        server.run(subprocess)

        request.addfinalizer(server.teardown)

        return server

    return run_test_server
