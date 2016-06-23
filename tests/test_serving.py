"""
    This module is adapted from Werkzeug's tests.serving module.

    :copyright: (c) 2014 by Armin Ronacher.
    :license: BSD, see COPYRIGHT-NOTICE for more details.
"""

import httplib

import requests


def test_server_actually_runs(test_server):
    server = test_server(
        """
        class Resource(object):
            def on_get(self, req, resp):
                resp.status = falcon.HTTP_200
                resp.body = 'Hello, world!'
        """
    )

    resp = requests.get('http://{}/resource'.format(server.addr))
    assert resp.content == 'Hello, world!'


def test_absolute_url_request(test_server):
    server = test_server(
        """
        class Resource(object):
            def on_get(self, req, resp):
                assert req.env['HTTP_HOST'] == 'notexisting.example.com:1337'
                assert req.env['PATH_INFO'] == '/resource'
                addr = req.env['HTTP_X_WERKZEUG_ADDR']
                assert req.env['SERVER_PORT'] == addr.split(':')[1]
                resp.status = falcon.HTTP_200
                resp.body = 'YAY'
        """
    )

    conn = httplib.HTTPConnection(server.addr)
    conn.request('GET', 'http://notexisting.example.com:1337/resource#ignore',
                 headers={'X-Werkzeug-Addr': server.addr})
    resp = conn.getresponse()
    assert resp.read() == 'YAY'


def test_broken_app_returns_500_response(test_server):
    server = test_server(
        """
        class Resource(object):
            def on_get(self, req, resp):
                assert False
        """
    )

    resp = requests.get('http://{}/resource'.format(server.addr))
    assert resp.status_code == 500
    assert 'Internal Server Error' in resp.text


def test_application_reloads_when_code_changes(test_server):
    app_string = """
        class Resource(object):
            def on_get(self, req, resp):
                resp.status = falcon.HTTP_200
                resp.body = '{}'

        kwargs['use_reloader'] = True
        kwargs['reloader_interval'] = 0.1
    """
    body1 = 'Hello, world!'
    body2 = 'Goodbye, cruel world!'

    server = test_server(app_string.format(body1))
    url = 'http://{}/resource'.format(server.addr)
    resp1 = requests.get(url)

    server.overwrite_application(app_string.format(body2))
    resp2 = requests.get(url)

    assert resp1.status_code == 200
    assert resp1.content == body1

    assert resp2.status_code == 200
    assert resp2.content == body2
