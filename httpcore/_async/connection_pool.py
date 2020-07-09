from ssl import SSLContext
from typing import AsyncIterator, Callable, Dict, List, Optional, Set, Tuple

from .._backends.auto import AsyncLock, AsyncSemaphore, AutoBackend
from .._exceptions import PoolTimeout
from .._threadlock import ThreadLock
from .._types import URL, Headers, Origin, TimeoutDict
from .._utils import get_logger, origin_to_url_string, url_to_origin
from .base import (
    AsyncByteStream,
    AsyncHTTPTransport,
    ConnectionState,
    NewConnectionRequired,
)
from .connection import AsyncHTTPConnection

logger = get_logger(__name__)


class NullSemaphore(AsyncSemaphore):
    def __init__(self) -> None:
        pass

    async def acquire(self, timeout: float = None) -> None:
        return

    def release(self) -> None:
        return


class ResponseByteStream(AsyncByteStream):
    def __init__(
        self,
        stream: AsyncByteStream,
        connection: AsyncHTTPConnection,
        callback: Callable,
    ) -> None:
        """
        A wrapper around the response stream that we return from `.request()`.

        Ensures that when `stream.aclose()` is called, the connection pool
        is notified via a callback.
        """
        self.stream = stream
        self.connection = connection
        self.callback = callback

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self.stream:
            yield chunk

    async def aclose(self) -> None:
        try:
            #  Call the underlying stream close callback.
            # This will be a call to `AsyncHTTP11Connection._response_closed()`
            # or `AsyncHTTP2Stream._response_closed()`.
            await self.stream.aclose()
        finally:
            #  Call the connection pool close callback.
            # This will be a call to `AsyncConnectionPool._response_closed()`.
            await self.callback(self.connection)


class AsyncConnectionPool(AsyncHTTPTransport):
    """
    A connection pool for making HTTP requests.

    **Parameters:**

    * **ssl_context** - `Optional[SSLContext]` - An SSL context to use for
    verifying connections.
    * **max_connections** - `Optional[int]` - The maximum number of concurrent
    connections to allow.
    * **max_keepalive** - `Optional[int]` - The maximum number of connections
    to allow before closing keep-alive connections.
    * **keepalive_expiry** - `Optional[float]` - The maximum time to allow
    before closing a keep-alive connection.
    * **http2** - `bool` - Enable HTTP/2 support.
    * **local_addr** - `Optional[bytes]` - Local address to connect from
    """

    def __init__(
        self,
        ssl_context: SSLContext = None,
        max_connections: int = None,
        max_keepalive: int = None,
        keepalive_expiry: float = None,
        http2: bool = False,
        local_addr: bytes = None,
    ):
        self._ssl_context = SSLContext() if ssl_context is None else ssl_context
        self._max_connections = max_connections
        self._max_keepalive = max_keepalive
        self._keepalive_expiry = keepalive_expiry
        self._http2 = http2
        self._local_addr = local_addr
        self._connections: Dict[Origin, Set[AsyncHTTPConnection]] = {}
        self._thread_lock = ThreadLock()
        self._backend = AutoBackend()
        self._next_keepalive_check = 0.0

    @property
    def _connection_semaphore(self) -> AsyncSemaphore:
        # We do this lazily, to make sure backend autodetection always
        # runs within an async context.
        if not hasattr(self, "_internal_semaphore"):
            if self._max_connections is not None:
                self._internal_semaphore = self._backend.create_semaphore(
                    self._max_connections, exc_class=PoolTimeout
                )
            else:
                self._internal_semaphore = NullSemaphore()

        return self._internal_semaphore

    @property
    def _connection_acquiry_lock(self) -> AsyncLock:
        if not hasattr(self, "_internal_connection_acquiry_lock"):
            self._internal_connection_acquiry_lock = self._backend.create_lock()
        return self._internal_connection_acquiry_lock

    async def request(
        self,
        method: bytes,
        url: URL,
        headers: Headers = None,
        stream: AsyncByteStream = None,
        timeout: TimeoutDict = None,
    ) -> Tuple[bytes, int, bytes, Headers, AsyncByteStream]:
        assert url[0] in (b"http", b"https")
        origin = url_to_origin(url)

        if self._keepalive_expiry is not None:
            await self._keepalive_sweep()

        connection: Optional[AsyncHTTPConnection] = None
        while connection is None:
            async with self._connection_acquiry_lock:
                # We get-or-create a connection as an atomic operation, to ensure
                # that HTTP/2 requests issued in close concurrency will end up
                # on the same connection.
                logger.trace("get_connection_from_pool=%r", origin)
                connection = await self._get_connection_from_pool(origin)

                if connection is None:
                    connection = AsyncHTTPConnection(
                        origin=origin,
                        http2=self._http2,
                        ssl_context=self._ssl_context,
                        local_addr=self._local_addr,
                    )
                    logger.trace("created connection=%r", connection)
                    await self._add_to_pool(connection, timeout=timeout)
                else:
                    logger.trace("reuse connection=%r", connection)

            try:
                response = await connection.request(
                    method, url, headers=headers, stream=stream, timeout=timeout
                )
            except NewConnectionRequired:
                connection = None
            except Exception:
                logger.trace("remove from pool connection=%r", connection)
                await self._remove_from_pool(connection)
                raise

        wrapped_stream = ResponseByteStream(
            response[4], connection=connection, callback=self._response_closed
        )
        return response[0], response[1], response[2], response[3], wrapped_stream

    async def _get_connection_from_pool(
        self, origin: Origin
    ) -> Optional[AsyncHTTPConnection]:
        # Determine expired keep alive connections on this origin.
        seen_http11 = False
        pending_connection = None
        reuse_connection = None
        connections_to_close = set()

        for connection in self._connections_for_origin(origin):
            if connection.is_http11:
                seen_http11 = True

            if connection.state == ConnectionState.IDLE:
                if connection.is_connection_dropped():
                    logger.trace("removing dropped idle connection=%r", connection)
                    # IDLE connections that have been dropped should be
                    # removed from the pool.
                    connections_to_close.add(connection)
                    await self._remove_from_pool(connection)
                else:
                    # IDLE connections that are still maintained may
                    # be reused.
                    logger.trace("reusing idle http11 connection=%r", connection)
                    reuse_connection = connection
            elif connection.state == ConnectionState.ACTIVE and connection.is_http2:
                # HTTP/2 connections may be reused.
                logger.trace("reusing active http2 connection=%r", connection)
                reuse_connection = connection
            elif connection.state == ConnectionState.PENDING:
                # Pending connections may potentially be reused.
                pending_connection = connection

        if reuse_connection is not None:
            # Mark the connection as READY before we return it, to indicate
            # that if it is HTTP/1.1 then it should not be re-acquired.
            reuse_connection.mark_as_ready()
            reuse_connection.expires_at = None
        elif self._http2 and pending_connection is not None and not seen_http11:
            # If we have a PENDING connection, and no HTTP/1.1 connections
            # on this origin, then we can attempt to share the connection.
            logger.trace("reusing pending connection=%r", connection)
            reuse_connection = pending_connection

        # Close any dropped connections.
        for connection in connections_to_close:
            await connection.aclose()

        return reuse_connection

    async def _response_closed(self, connection: AsyncHTTPConnection) -> None:
        remove_from_pool = False
        close_connection = False

        if connection.state == ConnectionState.CLOSED:
            remove_from_pool = True
        elif connection.state == ConnectionState.IDLE:
            num_connections = len(self._get_all_connections())
            if (
                self._max_keepalive is not None
                and num_connections > self._max_keepalive
            ):
                remove_from_pool = True
                close_connection = True
            elif self._keepalive_expiry is not None:
                now = self._backend.time()
                connection.expires_at = now + self._keepalive_expiry

        if remove_from_pool:
            await self._remove_from_pool(connection)

        if close_connection:
            await connection.aclose()

    async def _keepalive_sweep(self) -> None:
        """
        Remove any IDLE connections that have expired past their keep-alive time.
        """
        assert self._keepalive_expiry is not None

        now = self._backend.time()
        if now < self._next_keepalive_check:
            return

        self._next_keepalive_check = now + 1.0
        connections_to_close = set()

        for connection in self._get_all_connections():
            if (
                connection.state == ConnectionState.IDLE
                and connection.expires_at is not None
                and now > connection.expires_at
            ):
                connections_to_close.add(connection)
                await self._remove_from_pool(connection)

        for connection in connections_to_close:
            await connection.aclose()

    async def _add_to_pool(
        self, connection: AsyncHTTPConnection, timeout: TimeoutDict = None
    ) -> None:
        timeout = {} if timeout is None else timeout

        logger.trace("adding connection to pool=%r", connection)
        await self._connection_semaphore.acquire(timeout=timeout.get("pool", None))
        async with self._thread_lock:
            self._connections.setdefault(connection.origin, set())
            self._connections[connection.origin].add(connection)

    async def _remove_from_pool(self, connection: AsyncHTTPConnection) -> None:
        logger.trace("removing connection from pool=%r", connection)
        async with self._thread_lock:
            if connection in self._connections.get(connection.origin, set()):
                self._connection_semaphore.release()
                self._connections[connection.origin].remove(connection)
                if not self._connections[connection.origin]:
                    del self._connections[connection.origin]

    def _connections_for_origin(self, origin: Origin) -> Set[AsyncHTTPConnection]:
        return set(self._connections.get(origin, set()))

    def _get_all_connections(self) -> Set[AsyncHTTPConnection]:
        connections: Set[AsyncHTTPConnection] = set()
        for connection_set in self._connections.values():
            connections |= connection_set
        return connections

    async def aclose(self) -> None:
        connections = self._get_all_connections()
        for connection in connections:
            await self._remove_from_pool(connection)

        # Close all connections
        for connection in connections:
            await connection.aclose()

    def get_connection_info(self) -> Dict[str, List[str]]:
        """
        Returns a dict of origin URLs to a list of summary strings for each connection.
        """
        stats = {}
        for origin, connections in self._connections.items():
            stats[origin_to_url_string(origin)] = [
                connection.info() for connection in connections
            ]
        return stats
