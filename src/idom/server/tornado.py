from __future__ import annotations

import asyncio
import json
from asyncio import Queue as AsyncQueue
from asyncio.futures import Future
from concurrent.futures import ThreadPoolExecutor
from threading import Event as ThreadEvent
from typing import Any, List, Optional, Tuple, Type, Union
from urllib.parse import urljoin

from tornado.platform.asyncio import AsyncIOMainLoop
from tornado.web import Application, RedirectHandler, RequestHandler, StaticFileHandler
from tornado.websocket import WebSocketHandler
from typing_extensions import TypedDict

from idom.config import IDOM_WEB_MODULES_DIR
from idom.core.dispatcher import VdomJsonPatch, dispatch_single_view
from idom.core.layout import Layout, LayoutEvent
from idom.core.types import ComponentConstructor

from .utils import CLIENT_BUILD_DIR, threaded, wait_on_event


def configure(
    app: Application,
    component: ComponentConstructor,
    options: Options | None = None,
) -> TornadoServer:
    """Return a :class:`TornadoServer` where each client has its own state.

    Implements the :class:`~idom.server.proto.ServerFactory` protocol

    Parameters:
        app: A tornado ``Application`` instance.
        component: A root component constructor
        options: Options for configuring how the component is mounted to the server.
    """
    options = _setup_options(options)
    _add_handler(
        app,
        options,
        _setup_common_routes(options) + _setup_single_view_dispatcher_route(component),
    )
    return TornadoServer(app)


def create_development_app() -> Application:
    return Application(debug=True)


async def serve_development_app(
    app: Application, host: str, port: int, started: asyncio.Event
) -> None:
    loop = AsyncIOMainLoop()
    loop.install()
    app.listen(port, host)
    loop.add_callback(lambda: loop.asyncio_loop.call_soon_threadsafe(started.set))
    await loop.run_in_executor(ThreadPoolExecutor())


class Options(TypedDict, total=False):
    """Render server options for :class:`TornadoRenderServer` subclasses"""

    redirect_root_to_index: bool
    """Whether to redirect the root URL (with prefix) to ``index.html``"""

    serve_static_files: bool
    """Whether or not to serve static files (i.e. web modules)"""

    url_prefix: str
    """The URL prefix where IDOM resources will be served from"""


_RouteHandlerSpecs = List[Tuple[str, Type[RequestHandler], Any]]


class TornadoServer:
    """A thin wrapper for running a Tornado application

    See :class:`idom.server.proto.Server` for more info
    """

    _loop: asyncio.AbstractEventLoop

    def __init__(self, app: Application) -> None:
        self.app = app
        self._did_start = ThreadEvent()

    def run(self, host: str, port: int, *args: Any, **kwargs: Any) -> None:
        self._loop = asyncio.get_event_loop()
        AsyncIOMainLoop().install()
        self.app.listen(port, host, *args, **kwargs)
        self._did_start.set()
        asyncio.get_event_loop().run_forever()

    @threaded
    def run_in_thread(self, host: str, port: int, *args: Any, **kwargs: Any) -> None:
        self.run(host, port, *args, **kwargs)

    def wait_until_started(self, timeout: Optional[float] = 3.0) -> None:
        self._did_start.wait(timeout)

    def stop(self, timeout: Optional[float] = 3.0) -> None:
        try:
            loop = self._loop
        except AttributeError:  # pragma: no cover
            raise RuntimeError(
                f"Application is not running or was not started by {self}"
            )
        else:
            did_stop = ThreadEvent()

            def stop() -> None:
                loop.stop()
                did_stop.set()

            loop.call_soon_threadsafe(stop)

            wait_on_event(f"stop {self.app}", did_stop, timeout)


def _setup_options(options: Options | None) -> Options:
    return {
        "url_prefix": "",
        "serve_static_files": True,
        "redirect_root_to_index": True,
        **(options or {}),  # type: ignore
    }


def _setup_common_routes(options: Options) -> _RouteHandlerSpecs:
    handlers: _RouteHandlerSpecs = []
    if options["serve_static_files"]:
        handlers.append(
            (
                r"/client/(.*)",
                StaticFileHandler,
                {"path": str(CLIENT_BUILD_DIR)},
            )
        )
        handlers.append(
            (
                r"/modules/(.*)",
                StaticFileHandler,
                {"path": str(IDOM_WEB_MODULES_DIR.current)},
            )
        )
        if options["redirect_root_to_index"]:
            handlers.append(("/", RedirectHandler, {"url": "./client/index.html"}))
    return handlers


def _add_handler(
    app: Application, options: Options, handlers: _RouteHandlerSpecs
) -> None:
    prefixed_handlers: List[Any] = [
        (urljoin(options["url_prefix"], route_pattern),) + tuple(handler_info)
        for route_pattern, *handler_info in handlers
    ]
    app.add_handlers(r".*", prefixed_handlers)


def _setup_single_view_dispatcher_route(
    constructor: ComponentConstructor,
) -> _RouteHandlerSpecs:
    return [
        (
            "/stream",
            ModelStreamHandler,
            {"component_constructor": constructor},
        )
    ]


class ModelStreamHandler(WebSocketHandler):
    """A web-socket handler that serves up a new model stream to each new client"""

    _dispatch_future: Future[None]
    _message_queue: AsyncQueue[str]

    def initialize(self, component_constructor: ComponentConstructor) -> None:
        self._component_constructor = component_constructor

    async def open(self, *args: str, **kwargs: str) -> None:
        message_queue: "AsyncQueue[str]" = AsyncQueue()
        query_params = {k: v[0].decode() for k, v in self.request.arguments.items()}

        async def send(value: VdomJsonPatch) -> None:
            await self.write_message(json.dumps(value))

        async def recv() -> LayoutEvent:
            return LayoutEvent(**json.loads(await message_queue.get()))

        self._message_queue = message_queue
        self._dispatch_future = asyncio.ensure_future(
            dispatch_single_view(
                Layout(self._component_constructor(**query_params)),
                send,
                recv,
            )
        )

    async def on_message(self, message: Union[str, bytes]) -> None:
        await self._message_queue.put(
            message if isinstance(message, str) else message.decode()
        )

    def on_close(self) -> None:
        if not self._dispatch_future.done():
            self._dispatch_future.cancel()
