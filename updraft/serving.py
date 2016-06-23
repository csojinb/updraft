# -*- coding: utf-8 -*-
"""
    This module is adapted from the werkzeug.serving module, as are several of
    its dependencies.

    :copyright: (c) 2014 by the Werkzeug Team, see COPYRIGHT-NOTICE for more
    details.
    :license: BSD, see COPYRIGHT-NOTICE for more details.

    werkzeug.serving
    ~~~~~~~~~~~~~~~~

    There are many ways to serve a WSGI application.  While you're developing
    it you usually don't want a full blown webserver like Apache but a simple
    standalone one.  From Python 2.5 onwards there is the `wsgiref`_ server in
    the standard library.  If you're using older versions of Python you can
    download the package from the cheeseshop.

    However there are some caveats. Sourcecode won't reload itself when
    changed and each time you kill the server using ``^C`` you get an
    `KeyboardInterrupt` error.  While the latter is easy to solve the first
    one can be a pain in the ass in some situations.

    The easiest way is creating a small ``start-myproject.py`` that runs the
    application::

        #!/usr/bin/env python
        # -*- coding: utf-8 -*-
        from myproject import make_app
        from werkzeug.serving import run_simple

        app = make_app(...)
        run_simple('localhost', 8080, app, use_reloader=True)

    You can also pass it a `extra_files` keyword argument with a list of
    additional files (like configuration files) you want to observe.

    For bigger applications you should consider using `werkzeug.script`
    instead of a simple start file.


    :license: BSD, see LICENSE for more details.
"""
from __future__ import with_statement

import os
import socket
import sys
import ssl
import signal


def _get_openssl_crypto_module():
    try:
        from OpenSSL import crypto
    except ImportError:
        raise TypeError('Using ad-hoc certificates requires the pyOpenSSL '
                        'library.')
    else:
        return crypto


try:
    from SocketServer import ThreadingMixIn, ForkingMixIn
    from BaseHTTPServer import HTTPServer, BaseHTTPRequestHandler
except ImportError:
    from socketserver import ThreadingMixIn, ForkingMixIn
    from http.server import HTTPServer, BaseHTTPRequestHandler

from ._internal import _log
from ._compat import reraise, wsgi_encoding_dance
from .urls import url_parse, url_unquote
from .middleware import BlanketErrorHandlerMiddleware
from falcon.errors import HTTPInternalServerError
from falcon import API


class WSGIRequestHandler(BaseHTTPRequestHandler, object):

    """A request handler that implements WSGI dispatching."""

    # @property
    # def server_version(self):
    #     return 'Werkzeug/' + werkzeug.__version__

    def make_environ(self):
        request_url = url_parse(self.path)

        def shutdown_server():
            self.server.shutdown_signal = True

        url_scheme = self.server.ssl_context is None and 'http' or 'https'
        path_info = url_unquote(request_url.path)

        environ = {
            'wsgi.version':         (1, 0),
            'wsgi.url_scheme':      url_scheme,
            'wsgi.input':           self.rfile,
            'wsgi.errors':          sys.stderr,
            'wsgi.multithread':     self.server.multithread,
            'wsgi.multiprocess':    self.server.multiprocess,
            'wsgi.run_once':        False,
            'werkzeug.server.shutdown': shutdown_server,
            'SERVER_SOFTWARE':      self.server_version,
            'REQUEST_METHOD':       self.command,
            'SCRIPT_NAME':          '',
            'PATH_INFO':            wsgi_encoding_dance(path_info),
            'QUERY_STRING':         wsgi_encoding_dance(request_url.query),
            'CONTENT_TYPE':         self.headers.get('Content-Type', ''),
            'CONTENT_LENGTH':       self.headers.get('Content-Length', ''),
            'REMOTE_ADDR':          self.client_address[0],
            'REMOTE_PORT':          self.client_address[1],
            'SERVER_NAME':          self.server.server_address[0],
            'SERVER_PORT':          str(self.server.server_address[1]),
            'SERVER_PROTOCOL':      self.request_version
        }

        for key, value in self.headers.items():
            key = 'HTTP_' + key.upper().replace('-', '_')
            if key not in ('HTTP_CONTENT_TYPE', 'HTTP_CONTENT_LENGTH'):
                environ[key] = value

        if request_url.netloc:
            environ['HTTP_HOST'] = request_url.netloc

        return environ

    def run_wsgi(self):
        if self.headers.get('Expect', '').lower().strip() == '100-continue':
            self.wfile.write(b'HTTP/1.1 100 Continue\r\n\r\n')

        self.environ = environ = self.make_environ()
        headers_set = []
        headers_sent = []

        def write(data):
            assert headers_set, 'write() before start_response'
            if not headers_sent:
                status, response_headers = headers_sent[:] = headers_set
                try:
                    code, msg = status.split(None, 1)
                except ValueError:
                    code, msg = status, ""
                self.send_response(int(code), msg)
                header_keys = set()
                for key, value in response_headers:
                    self.send_header(key, value)
                    key = key.lower()
                    header_keys.add(key)
                if 'content-length' not in header_keys:
                    self.close_connection = True
                    self.send_header('Connection', 'close')
                if 'server' not in header_keys:
                    self.send_header('Server', self.version_string())
                if 'date' not in header_keys:
                    self.send_header('Date', self.date_time_string())
                self.end_headers()

            assert isinstance(data, bytes), 'applications must write bytes'
            self.wfile.write(data)
            self.wfile.flush()

        def start_response(status, response_headers, exc_info=None):
            if exc_info:
                try:
                    if headers_sent:
                        reraise(*exc_info)
                finally:
                    exc_info = None
            elif headers_set:
                raise AssertionError('Headers already set')
            headers_set[:] = [status, response_headers]
            return write

        def execute(app):
            application_iter = app(environ, start_response)
            try:
                for data in application_iter:
                    write(data)
                if not headers_sent:
                    write(b'')
            finally:
                if hasattr(application_iter, 'close'):
                    application_iter.close()
                application_iter = None

        try:
            execute(self.server.app)
        except Exception:
            import traceback
            tb = traceback.format_exc()
            self.server.log('error', 'Error on request:\n%s', tb)
            raise

    def handle(self):
        """Handles a request ignoring dropped connections."""
        rv = None
        try:
            rv = BaseHTTPRequestHandler.handle(self)
        except (socket.error, socket.timeout) as e:
            self.connection_dropped(e)
        except Exception:
            if self.server.ssl_context is None or not is_ssl_error():
                raise
        if self.server.shutdown_signal:
            self.initiate_shutdown()
        return rv

    def initiate_shutdown(self):
        """A horrible, horrible way to kill the server for Python 2.6 and
        later.  It's the best we can do.
        """
        # Windows does not provide SIGKILL, go with SIGTERM then.
        sig = getattr(signal, 'SIGKILL', signal.SIGTERM)
        # reloader active
        if os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
            os.kill(os.getpid(), sig)
        # python 2.7
        self.server._BaseServer__shutdown_request = True
        # python 2.6
        self.server._BaseServer__serving = False

    def connection_dropped(self, error, environ=None):
        """Called if the connection was closed by the client.  By default
        nothing happens.
        """

    def handle_one_request(self):
        """Handle a single HTTP request."""
        self.raw_requestline = self.rfile.readline()
        if not self.raw_requestline:
            self.close_connection = 1
        elif self.parse_request():
            return self.run_wsgi()

    def send_response(self, code, message=None):
        """Send the response header and log the response code."""
        self.log_request(code)
        if message is None:
            message = code in self.responses and self.responses[code][0] or ''
        if self.request_version != 'HTTP/0.9':
            hdr = "%s %d %s\r\n" % (self.protocol_version, code, message)
            self.wfile.write(hdr.encode('ascii'))

    def version_string(self):
        return BaseHTTPRequestHandler.version_string(self).strip()

    def address_string(self):
        return self.environ['REMOTE_ADDR']

    def log_request(self, code='-', size='-'):
        self.log('info', '"%s" %s %s', self.requestline, code, size)

    def log_error(self, *args):
        self.log('error', *args)

    def log_message(self, format, *args):
        self.log('info', format, *args)

    def log(self, type, message, *args):
        _log(type, '%s - - [%s] %s\n' % (self.address_string(),
                                         self.log_date_time_string(),
                                         message % args))


def generate_adhoc_ssl_pair(cn=None):
    from random import random
    crypto = _get_openssl_crypto_module()

    # pretty damn sure that this is not actually accepted by anyone
    if cn is None:
        cn = '*'

    cert = crypto.X509()
    cert.set_serial_number(int(random() * sys.maxsize))
    cert.gmtime_adj_notBefore(0)
    cert.gmtime_adj_notAfter(60 * 60 * 24 * 365)

    subject = cert.get_subject()
    subject.CN = cn
    subject.O = 'Dummy Certificate'

    issuer = cert.get_issuer()
    issuer.CN = 'Untrusted Authority'
    issuer.O = 'Self-Signed'

    pkey = crypto.PKey()
    pkey.generate_key(crypto.TYPE_RSA, 1024)
    cert.set_pubkey(pkey)
    cert.sign(pkey, 'md5')

    return cert, pkey


def generate_adhoc_ssl_context():
    """Generates an adhoc SSL context for the development server."""
    crypto = _get_openssl_crypto_module()
    import tempfile
    import atexit

    cert, pkey = generate_adhoc_ssl_pair()
    cert_handle, cert_file = tempfile.mkstemp()
    pkey_handle, pkey_file = tempfile.mkstemp()
    atexit.register(os.remove, pkey_file)
    atexit.register(os.remove, cert_file)

    os.write(cert_handle, crypto.dump_certificate(crypto.FILETYPE_PEM, cert))
    os.write(pkey_handle, crypto.dump_privatekey(crypto.FILETYPE_PEM, pkey))
    os.close(cert_handle)
    os.close(pkey_handle)
    ctx = load_ssl_context(cert_file, pkey_file)
    return ctx


def load_ssl_context(cert_file, pkey_file=None, protocol=None):
    """Loads SSL context from cert/private key files and optional protocol.
    Many parameters are directly taken from the API of
    :py:class:`ssl.SSLContext`.

    :param cert_file: Path of the certificate to use.
    :param pkey_file: Path of the private key to use. If not given, the key
                      will be obtained from the certificate file.
    :param protocol: One of the ``PROTOCOL_*`` constants in the stdlib ``ssl``
                     module. Defaults to ``PROTOCOL_SSLv23``.
    """
    if protocol is None:
        protocol = ssl.PROTOCOL_SSLv23
    ctx = _SSLContext(protocol)
    ctx.load_cert_chain(cert_file, pkey_file)
    return ctx


class _SSLContext(object):

    '''A dummy class with a small subset of Python3's ``ssl.SSLContext``, only
    intended to be used with and by Werkzeug.'''

    def __init__(self, protocol):
        self._protocol = protocol
        self._certfile = None
        self._keyfile = None
        self._password = None

    def load_cert_chain(self, certfile, keyfile=None, password=None):
        self._certfile = certfile
        self._keyfile = keyfile or certfile
        self._password = password

    def wrap_socket(self, sock, **kwargs):
        return ssl.wrap_socket(sock, keyfile=self._keyfile,
                               certfile=self._certfile,
                               ssl_version=self._protocol, **kwargs)


def is_ssl_error(error=None):
    """Checks if the given error (or the current one) is an SSL error."""
    exc_types = (ssl.SSLError,)
    try:
        from OpenSSL.SSL import Error
        exc_types += (Error,)
    except ImportError:
        pass

    if error is None:
        error = sys.exc_info()[1]
    return isinstance(error, exc_types)


def select_ip_version(host, port):
    """Returns AF_INET4 or AF_INET6 depending on where to connect to."""
    # disabled due to problems with current ipv6 implementations
    # and various operating systems.  Probably this code also is
    # not supposed to work, but I can't come up with any other
    # ways to implement this.
    # try:
    #     info = socket.getaddrinfo(host, port, socket.AF_UNSPEC,
    #                               socket.SOCK_STREAM, 0,
    #                               socket.AI_PASSIVE)
    #     if info:
    #         return info[0][0]
    # except socket.gaierror:
    #     pass
    if ':' in host and hasattr(socket, 'AF_INET6'):
        return socket.AF_INET6
    return socket.AF_INET


class BaseWSGIServer(HTTPServer, object):

    """Simple single-threaded, single-process WSGI server."""
    multithread = False
    multiprocess = False
    request_queue_size = 128

    def __init__(self, host, port, app, handler=None,
                 ssl_context=None):
        if handler is None:
            handler = WSGIRequestHandler
        self.address_family = select_ip_version(host, port)
        HTTPServer.__init__(self, (host, int(port)), handler)
        self.app = app
        self.shutdown_signal = False

        if ssl_context is not None:
            if isinstance(ssl_context, tuple):
                ssl_context = load_ssl_context(*ssl_context)
            if ssl_context == 'adhoc':
                ssl_context = generate_adhoc_ssl_context()
            self.socket = ssl_context.wrap_socket(self.socket,
                                                  server_side=True)
            self.ssl_context = ssl_context
        else:
            self.ssl_context = None

    def log(self, type, message, *args):
        _log(type, message, *args)

    def serve_forever(self):
        self.shutdown_signal = False
        try:
            HTTPServer.serve_forever(self)
        except KeyboardInterrupt:
            pass
        finally:
            self.server_close()

    def handle_error(self, request, client_address):
        return HTTPServer.handle_error(self, request, client_address)

    def get_request(self):
        con, info = self.socket.accept()
        return con, info


def make_server(host, port, app=None, request_handler=None):
    """Create a new server instance."""
    return BaseWSGIServer(host, port, app, request_handler)


def is_running_from_reloader():
    """Checks if the application is running from within the Werkzeug
    reloader subprocess.

    .. versionadded:: 0.10
    """
    return os.environ.get('WERKZEUG_RUN_MAIN') == 'true'


def run_simple(hostname, port, application, use_reloader=False,
               use_debugger=False, extra_files=None, reloader_interval=1,
               debug_method=None):
    """Start a WSGI application. Optional features include a reloader,
    multithreading and fork support.

    This function has a command-line interface too::

        python -m werkzeug.serving --help

    .. versionadded:: 0.5
       `static_files` was added to simplify serving of static files as well
       as `passthrough_errors`.

    .. versionadded:: 0.6
       support for SSL was added.

    .. versionadded:: 0.8
       Added support for automatically loading a SSL context from certificate
       file and private key.

    .. versionadded:: 0.9
       Added command-line interface.

    .. versionadded:: 0.10
       Improved the reloader and added support for changing the backend
       through the `reloader_type` parameter.  See :ref:`reloader`
       for more information.

    :param hostname: The host for the application.  eg: ``'localhost'``
    :param port: The port for the server.  eg: ``8080``
    :param application: the WSGI application to execute
    :param use_reloader: should the server automatically restart the python
                         process if modules were changed?
    :param use_debugger: should the werkzeug debugging system be used?
    :param extra_files: a list of files the reloader should watch
                        additionally to the modules.  For example configuration
                        files.
    :param reloader_interval: the interval for the reloader in seconds.
    :param debug_method:
    """
    if use_debugger:
        from .middleware import PdbDebugMiddleware
        kwargs = {'debug_method': debug_method} if debug_method else {}
        application = PdbDebugMiddleware(application, **kwargs)
    else:
        application = BlanketErrorHandlerMiddleware(application)

    def inner():
        make_server(hostname, port, application).serve_forever()

    if os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
        display_hostname = hostname != '*' and hostname or 'localhost'
        if ':' in display_hostname:
            display_hostname = '[%s]' % display_hostname
        quit_msg = '(Press CTRL+C to quit)'
        _log('info', ' * Running on http://{}:{}/ {}'.format(
            display_hostname, port, quit_msg))

    if use_reloader:
        # Create and destroy a socket so that any exceptions are raised before
        # we spawn a separate Python interpreter and lose this ability.
        address_family = select_ip_version(hostname, port)
        test_socket = socket.socket(address_family, socket.SOCK_STREAM)
        test_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        test_socket.bind((hostname, port))
        test_socket.close()

        reloader_type = 'auto'

        from ._reloader import run_with_reloader
        run_with_reloader(inner, extra_files, reloader_interval,
                          reloader_type)
    else:
        inner()
