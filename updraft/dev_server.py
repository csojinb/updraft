from .serving import run_simple


def run_dev_server(api, hostname='127.0.0.1', port=5000,
                   use_reloader=True, use_debugger=False):
    """Runs a development WSGI server for a falcon application

    Args:
        api: A falcon API object
        hostname (str, optional): The host for the application. Defaults to
            `'127.0.0.1'`
        port (int, optional): The port for the server. Defaults to `5000`
        use_reloader (bool, optional): Should the server automatically restart
            when the application code changes? Defaults to `True`
        use_debugger (bool, optional): Should the server drop you into `pdb`
            on exception? Defaults to `False`

    """
    run_simple(hostname, port, api, use_reloader=use_reloader,
               use_debugger=use_debugger)
