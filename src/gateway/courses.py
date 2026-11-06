"""Именованные курсы с независимым состоянием на одном HTTPS origin."""
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Literal
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import Field, model_validator
from .config import Closed, Configuration
from .errors import GatewayError
from .app import create_app

class CoursesConfiguration(Closed):
    schemaVersion: Literal[2]
    courses: dict[str, Configuration] = Field(min_length=1)

    @model_validator(mode='after')
    def isolated(self):
        databases=set(); origins=set(); activities=set(); native_bindings=set()
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

def create_courses_app(config, *, factory=create_app):
    if not isinstance(config,CoursesConfiguration): config=CoursesConfiguration.model_validate(config)
    children={name:factory(course) for name,course in config.courses.items()}
    @asynccontextmanager
    async def lifespan(app):
        async with AsyncExitStack() as stack:
            for child in children.values(): await stack.enter_async_context(child.router.lifespan_context(child))
            yield
    app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.courses=children
    @app.exception_handler(GatewayError)
    async def rejected(request,error):
        return JSONResponse({'detail':str(error)},status_code=error.status,headers={'Cache-Control':'no-store'})
    @app.middleware('http')
    async def security(request,call_next):
        response=await call_next(request)
        response.headers.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer'})
        return response
    for child in children.values(): app.router.routes.extend(child.router.routes)
    @app.api_route('/gateway/lti/login',methods=['GET','POST'])
    async def shared_login(request:Request):
        params=dict(await request.form()) if request.method=='POST' else dict(request.query_params)
        selected=[name for name,course in config.courses.items() if params.get('target_link_uri')==course.publicOrigin.rstrip('/')+course.routePrefix+'/lti/launch']
        if len(selected)!=1: raise GatewayError('Неизвестный зарегистрированный target',403)
        return await children[selected[0]].state.login_handler(request)
    @app.get('/internal/handoff/courses/{unknown}/{token}')
    async def unknown_course(unknown:str,token:str):
        raise GatewayError('Неизвестный курс handoff',403)
    return app
