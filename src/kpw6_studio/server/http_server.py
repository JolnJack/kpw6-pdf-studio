"""HTTP serving for the desktop app's loopback address.

SPDX-License-Identifier: AGPL-3.0-only
"""
from socketserver import TCPServer

from werkzeug.serving import ThreadedWSGIServer


class LoopbackWSGIServer(ThreadedWSGIServer):
    def server_bind(self):
        # HTTPServer.server_bind performs reverse DNS through getfqdn. A
        # desktop-only loopback service already knows its host; DNS availability
        # must not hold up socket activation and the launcher's health check.
        TCPServer.server_bind(self)
        self.server_name = self.server_address[0]
        self.server_port = self.server_address[1]
