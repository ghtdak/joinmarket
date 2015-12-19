from __future__ import absolute_import, print_function

import base64
import httplib
import json
from collections import defaultdict, deque

import treq
from twisted.internet import defer, reactor
from twisted.logger import Logger

"""
Copyright (C) 2013,2015 by Daniel Kraft <d@domob.eu>
Copyright (C) 2014 by phelix / blockchained.com

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

log = Logger()

tb_stack_dd = defaultdict(int)


class JsonRpcError(Exception):
    """
    The called method returned an error in the JSON-RPC response.
    """

    def __init__(self, obj):
        self.code = obj["code"]
        self.message = obj["message"]


class JsonRpcConnectionError(Exception):
    """
    Error thrown when the RPC connection itself failed.  This means
    that the server is either down or the connection settings
    are wrong.
    """

    pass


class JsonRpc(object):
    """
    Simple implementation of a JSON-RPC client that is used
    to connect to Bitcoin.
    """

    def __init__(self, host, port, user, password):
        self.host = host
        self.port = port
        self.authstr = '{}:{}'.format(user, password)
        self.headers = {'User-Agent': 'joinmarket',
                        'Content-Type': 'application/json',
                        'Accept': 'application/json'}
        self.headers['Authorization'] = 'Basic {}'.format(base64.b64encode(
                self.authstr))
        self.url = 'http://{}:{}'.format(host, port)
        self.queryId = 1
        self.asyncQ = deque()
        self.asyncCount = 0
        self.blockNew = 0

    def treq_queryHTTP(self, obj):
        self.blockNew += 1
        if self.asyncCount > 10:
            log.debug('treq_queryHTTP blocking - Q size: {:d}'.format(
                    self.asyncCount))
        while self.asyncCount > 0:
            reactor.runUntilCurrent()
            reactor.doIteration(.001)

        ret_whatevername = []

        def callMe(response):
            if response:
                ret_whatevername.append(response)
            else:
                ret_whatevername.append('')

        d = self.post_defer(obj)
        d.addCallback(callMe)
        self.blockNew -= 1

        while len(ret_whatevername) == 0:
            reactor.runUntilCurrent()
            reactor.doIteration(.001)
        return ret_whatevername.pop()

    def queryHTTP(self, obj):
        """
    Send an appropriate HTTP query to the server.  The JSON-RPC
    request should be (as object) in 'obj'.  If the call succeeds,
    the resulting JSON object is returned.  In case of an error
    with the connection (not JSON-RPC itself), an exception is raised.
    """

        body = json.dumps(obj)

        # black magic assist.  if there are async calls still queued, wait

        self.blockNew += 1
        while self.asyncCount > 0:
            reactor.iterate()
        self.blockNew -= 1

        try:
            conn = httplib.HTTPConnection(self.host, self.port)
            conn.request("POST", "", body, self.headers)
            response = conn.getresponse()

            if response.status == 401:
                conn.close()
                raise JsonRpcConnectionError(
                        "authentication for JSON-RPC failed")

            # All of the codes below are 'fine' from a JSON-RPC point of view.
            if response.status not in [200, 404, 500]:
                conn.close()
                raise JsonRpcConnectionError("unknown error in JSON-RPC")

            data = response.read()
            conn.close()

            return json.loads(data)

        except JsonRpcConnectionError as exc:
            raise exc
        except Exception as exc:
            raise JsonRpcConnectionError(
                    "JSON-RPC connection failed. Err: {}".format(exc))

    @defer.inlineCallbacks
    def post_defer(self, obj):
        self.asyncCount += 1
        js = {'jm_error': 'inialize'}
        content = None
        try:
            body = json.dumps(obj)
            response = yield treq.post(self.url,
                                       data=body,
                                       headers=self.headers)

            if response.code not in [200, 404, 500]:
                log.error('Unknown error in JsonRpc - post-defer: {}'.format(
                        response.code))

            # todo: for debugging.  Can be done with a single call
            content = yield response.content()
            js = json.loads(content)
        except:
            log.debug('conversion exception', content=content)
            print('conversion exception: ', content)
            js = {'jm_error': 'exception',
                  'jm_content': content,
                  'error': 'error'}
        else:
            # log.debug('json conversion success: {}'.format(js))
            if js['id'] != obj['id']:
                log.error('post_defer id', js=js)
                print(js)
                js['jm_error'] = 'id_mismatch'

        # todo: deal with exceptions properly
        finally:
            self.asyncCount -= 1
            defer.returnValue(js)

    def call(self, method, params, immediate=False):

        # todo: call stack monitoring
        # tb_stack_dd[tuple(traceback.extract_stack())] += 1

        currentId = self.queryId
        self.queryId += 1

        request = {"method": method, "params": params, "id": currentId}

        # if reactor.running and immediate:
        #     return self.queuePost(request)
        #
        # if reactor.running:
        #     response = self.treq_queryHTTP(request)
        # else:
        #     response = self.queryHTTP(request)

        response = self.queryHTTP(request)

        if response["id"] != currentId:
            print('jsonrpc: {}'.format(response))
            raise JsonRpcConnectionError("invalid id returned by query")

        if response["error"] is not None:
            # todo: could be a warning or error
            print(response["error"], request)
            print('jsonrpc: {} {}'.format(response, request))
            raise JsonRpcError(response["error"])

        return response["result"]

    def intercept(self, response, calld):
        if len(self.asyncQ) > 0:
            request, nd = self.asyncQ.popleft()
            rd = self.post_defer(request)
            rd.addCallback(self.intercept, nd)

        self.asyncCount -= 1
        calld.callback(response)

    def queuePost(self, request):

        nd = defer.Deferred()

        if self.asyncCount <= 0 and self.blockNew == 0:
            rd = self.post_defer(request)
            rd.addCallback(self.intercept, nd)
        else:
            self.asyncQ.append((request, nd))

        self.asyncCount += 1

        return nd
