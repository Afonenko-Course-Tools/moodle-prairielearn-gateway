"""Именованные курсы с независимым состоянием на одном HTTPS origin."""
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Literal
from collections.abc import AsyncGenerator, Callable
from starlette.middleware.base import RequestResponseEndpoint
from fastapi.responses import Response, RedirectResponse
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import Field, model_validator
from .config import Closed, Configuration
from .errors import GatewayError
from .app import GatewayApplication, create_app, request_params

class CoursesConfiguration(Closed):
    schemaVersion: Literal[2]
    courses: dict[str, Configuration] = Field(min_length=1)

    @model_validator(mode='after')
    def isolated(self) -> "CoursesConfiguration":
        databases: set[str] = set(); origins: set[str] = set(); activities: set[tuple[str, str, str, str, str]] = set(); native_bindings: set[tuple[str, str]] = set()
        for name, course in self.courses.items():
            if course.routePrefix != '/gateway/courses/'+name:
                raise ValueError('Ключ курса должен совпадать с routePrefix')
            database=str(Path(course.database).resolve())
            binding=(course.instance,course.courseKey)
            if database in databases or binding in native_bindings:
                raise ValueError('Курсам нужны отдельные auth DB и native bindings')
            databases.add(database); native_bindings.add(binding)
            origins.add(course.publicOrigin.rstrip('/'))
            reg=course.registration
            for activity in reg.activities.values():
                key=(reg.issuer,reg.clientId,reg.deploymentId,reg.contextId,activity.resourceLinkId)
                if key in activities:
                    raise ValueError('LTI resource не может принадлежать двум native курсам')
                activities.add(key)
        if len(origins)!=1:
            raise ValueError('Курсам нужен один public origin')
        if len({str(Path(c.handoffTokenFile).resolve()) for c in self.courses.values()})!=1:
            raise ValueError('Курсам нужен общий proxy handoff service token')
        return self

def create_courses_app(config: object, *, factory: Callable[[Configuration], GatewayApplication] = create_app) -> FastAPI:
    if not isinstance(config,CoursesConfiguration): config=CoursesConfiguration.model_validate(config)
    children={name:factory(course) for name,course in config.courses.items()}
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        async with AsyncExitStack() as stack:
            for child in children.values(): await stack.enter_async_context(child.router.lifespan_context(child))
            yield
    app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.courses=children
    @app.exception_handler(GatewayError)
    async def rejected(request: Request, error: Exception) -> JSONResponse:
        if not isinstance(error, GatewayError): raise error
        return JSONResponse({'detail':str(error)},status_code=error.status,headers={'Cache-Control':'no-store'})
    @app.middleware('http')
    async def security(request: Request, call_next: RequestResponseEndpoint) -> Response:
        response=await call_next(request)
        response.headers.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer'})
        return response
    for child in children.values(): app.router.routes.extend(child.router.routes)
    @app.api_route('/gateway/lti/login',methods=['GET','POST'])
    async def shared_login(request:Request) -> RedirectResponse:
        params=await request_params(request)
        selected=[name for name,course in config.courses.items() if params.get('target_link_uri')==course.publicOrigin.rstrip('/')+course.routePrefix+'/lti/launch']
        if len(selected)!=1: raise GatewayError('Неизвестный зарегистрированный target',403)
        return await children[selected[0]].login_handler(request)
    @app.get('/internal/handoff/courses/{unknown}/{token}')
    async def unknown_course(unknown:str,token:str) -> None:
        raise GatewayError('Неизвестный курс handoff',403)
    return app
