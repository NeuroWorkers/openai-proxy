#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from flask import Flask, request, Response
import requests
import logging
from datetime import datetime

LOG_FILE_PATH = '/var/log/openai-proxy-requests.log'
ALLOWED_IPS = ['81.22.48.239', '45.43.88.234']
IGNORE_REQUEST_HEADERS = ['Host', 'Accept-Encoding']

logging.basicConfig(
    filename = LOG_FILE_PATH,
    level = logging.INFO,
    format = '%(message)s'
)

class SSEClient(object):
    """Implementation of a SSE client.
    See http://www.w3.org/TR/2009/WD-eventsource-20091029/ for the
    specification.
    """

    def __init__(self, event_source, char_enc='utf-8'):
        """Initialize the SSE client over an existing, ready to consume
        event source.
        The event source is expected to be a binary stream and have a close()
        method. That would usually be something that implements
        io.BinaryIOBase, like an httplib or urllib3 HTTPResponse object.
        """
        self._logger = logging.getLogger(self.__class__.__module__)
        self._logger.debug('Initialized SSE client from event source %s',
                           event_source)
        self._event_source = event_source
        self._char_enc = char_enc

    def _read(self):
        """Read the incoming event source stream and yield event chunks.
        Unfortunately it is possible for some servers to decide to break an
        event into multiple HTTP chunks in the response. It is thus necessary
        to correctly stitch together consecutive response chunks and find the
        SSE delimiter (empty new line) to yield full, correct event chunks."""
        data = b''
        for chunk in self._event_source:
            for line in chunk.splitlines(True):
                data += line
                if data.endswith((b'\r\r', b'\n\n', b'\r\n\r\n')):
                    yield data
                    data = b''
        if data:
            yield data

    def events(self):
        for chunk in self._read():
            event = Event()
            # Split before decoding so splitlines() only uses \r and \n
            for line in chunk.splitlines():
                # Decode the line.
                line = line.decode(self._char_enc)

                # Lines starting with a separator are comments and are to be
                # ignored.
                if not line.strip() or line.startswith(':'):
                    continue

                data = line.split(':', 1)
                field = data[0]

                # Ignore unknown fields.
                if field not in event.__dict__:
                    self._logger.debug('Saw invalid field %s while parsing '
                                       'Server Side Event', field)
                    continue

                if len(data) > 1:
                    # From the spec:
                    # "If value starts with a single U+0020 SPACE character,
                    # remove it from value."
                    if data[1].startswith(' '):
                        value = data[1][1:]
                    else:
                        value = data[1]
                else:
                    # If no value is present after the separator,
                    # assume an empty value.
                    value = ''

                # The data field may come over multiple lines and their values
                # are concatenated with each other.
                if field == 'data':
                    event.__dict__[field] += value + '\n'
                else:
                    event.__dict__[field] = value

            # Events with no data are not dispatched.
            if not event.data:
                continue

            # If the data field ends with a newline, remove it.
            if event.data.endswith('\n'):
                event.data = event.data[0:-1]

            # Empty event names default to 'message'
            event.event = event.event or 'message'

            # Dispatch the event
            self._logger.debug('Dispatching %s...', event)
            yield event

    def close(self):
        """Manually close the event source stream."""
        self._event_source.close()


class Event(object):
    """Representation of an event from the event stream."""

    def __init__(self, id=None, event='message', data='', retry=None):
        self.id = id
        self.event = event
        self.data = data
        self.retry = retry

    def __str__(self):
        s = '{0} event'.format(self.event)
        if self.id:
            s += ' #{0}'.format(self.id)
        if self.data:
            s += ', {0} byte{1}'.format(len(self.data),
                                        's' if len(self.data) else '')
        else:
            s += ', no data'
        if self.retry:
            s += ', retry in {0}ms'.format(self.retry)
        return s


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_orig_request( request ):
    log_message = f"[{now()}] REQUEST:\n"
    log_message += f"Method: {request.method}\n"
    log_message += f"URL: {request.url}\n"
    log_message += "Headers:\n"
    for key, value in request.headers.items():
        log_message += f"{key}: {value}\n"
    log_message += f"Body:\n{request.data.decode('utf-8', errors='ignore')}\n\n"
    logging.info( log_message )


def log_our_request( prepared ):
    log_message = f"[{now()}] OUR REQUEST:\n"
    log_message += f"Method: {prepared.method}\n"
    log_message += f"URL: {prepared.url}\n"
    log_message += "Headers:\n"
    for header, value in prepared.headers.items():
        log_message += f"{header}: {value}\n"
    logging.info( log_message )


def log_response(response):
    log_message = f"[{now()}] RESPONSE Status: {response.status_code}:\n"

    log_message += "Headers:\n"
    headers_dict = {}
    for key, value in response.raw.headers.items():
        log_message += f"{key}: {value}\n"
        headers_dict[key.lower()] = value

    logging.info( log_message )
    logging.info( type(response.content) )

    content_encoding = headers_dict.get('content-encoding')
    body = response.content

    # Поддержка сжатого содержимого
    if content_encoding == 'br':
        import brotlicffi
        decompressed_body = brotlicffi.decompress(body)
    elif content_encoding == 'gzip':
        import gzip
        decompressed_body = gzip.decompress(body)
    elif content_encoding == 'deflate':
        import zlib
        decompressed_body = zlib.decompress(body)
    else:
        decompressed_body = body    

    body_text = decompressed_body.decode('utf-8', errors='ignore')

    log_message = f"Body:\n{body_text}\n\n"
    logging.info( log_message )


app = Flask(__name__)
# Список разрешенных IP-адресов

@app.before_request
def limit_remote_addr():
    if request.remote_addr not in ALLOWED_IPS:
        abort(403)  # Возвращаем ошибку 403 Forbidden

# @app.after_request
# def after_request(response):
#     # Печать информации об ответе
#     print("Response status:", response.status)
#     print("Response headers:", response.headers)
#     print("Response length:", len(response.get_data()))
#     # Возвращаем ответ, чтобы он был отправлен клиенту
#     return response

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE'])
def proxy(path):
    log_orig_request( request )

    url = request.url.replace( request.host_url, 'https://api.openai.com/' )
    stream = None
    try:
        stream = request.get_json().get('stream', None)
    except:
        pass

    # Фильтрация заголовков, исключая нежелательные
    filtered_headers = {
        key: value for key, value in request.headers.items()
        if key not in IGNORE_REQUEST_HEADERS
    }

    req = requests.Request(
        method = request.method,
        url = url,
        headers = filtered_headers,
        data = request.get_data(),
    )

    prepared_req = req.prepare()
    log_our_request( prepared_req )

    session = requests.Session()
    resp = session.send( prepared_req, stream = stream, allow_redirects = False )

    log_response( resp )

    if not stream:
        excluded_headers = ['content-length', 'connection']
        headers = [
            (name, value) for (name, value) in resp.raw.headers.items() \
                if name.lower() not in excluded_headers
        ]
        response = app.make_response((resp.content, resp.status_code, headers))
        return response

    def stream_generate():
        client = SSEClient(resp)
        for event in client.events():
            yield ('data: ' + event.data + '\n\n')
    return Response(stream_generate(), mimetype='text/event-stream')


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=9000)
