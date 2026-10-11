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
        databases=set(); deliveries=set(); origins=set(); activities=set(); client_keys={}
        for name, course in self.courses.items():
            if course.routePrefix != '/gateway/courses/'+name: raise ValueError('Course key must match its fixed route prefix')
            database=str(Path(course.database).resolve())
            delivery=str(Path(course.deliveryPath).resolve())
            if database in databases or delivery in deliveries: raise ValueError('Courses require separate database and delivery paths')
            databases.add(database);deliveries.add(delivery);origins.add(course.publicOrigin.rstrip('/'))
            reg=course.registration
            scope=(reg.issuer,reg.clientId,reg.deploymentId)
            key=str(Path(course.privateKeyFile).resolve())
            if scope in client_keys and client_keys[scope]!=key: raise ValueError('One LTI client must use one signing key')
            client_keys[scope]=key
            for activity in reg.activities.values():
                key=(reg.issuer,reg.clientId,reg.deploymentId,reg.contextId,activity.resourceLinkId)
                if key in activities: raise ValueError('LTI resource cannot belong to two native courses')
                activities.add(key)
        if len(origins)!=1: raise ValueError('Courses require one public origin')
        if len({str(Path(c.handoffTokenFile).resolve()) for c in self.courses.values()})!=1: raise ValueError('Named courses require the shared proxy handoff service token')
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
        if len(selected)!=1: raise GatewayError('Unknown registered target',403)
        return await children[selected[0]].state.login_handler(request)
    @app.get('/internal/handoff/courses/{unknown}/{token}')
    async def unknown_course(unknown:str,token:str):
        raise GatewayError('Unknown course handoff',403)
    return app
