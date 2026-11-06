"""Имя и email передаются только из подписанного immutable snapshot."""
import base64
import json
import pytest
from gateway.app import create_app, verified_name_header
from gateway.errors import LaunchError
from test_auth_flow import configuration, Platform, browser_for, activate, handoff, UID
from test_launch import env, attempt

MISSING=object()


@pytest.mark.parametrize('email,verified,expected',[
    (' Owner@Example.Invalid ',MISSING,'owner@example.invalid'),('owner@example.invalid',True,'owner@example.invalid'),
    ('owner+alias@example.invalid',MISSING,'owner+alias@example.invalid'),
    (MISSING,MISSING,None),(None,MISSING,None),(42,MISSING,None),
    ('owner@example.invalid',False,None),('owner@example.invalid','true',None),('owner@example.invalid',1,None),
    ('owner@example.invalid',None,None),('owner@example.invalid\n',MISSING,None),('\towner@example.invalid',MISSING,None),
    ('ownér@example.invalid',MISSING,None),('owner@exаmple.invalid',MISSING,None),
    ('owner@@example.invalid',MISSING,None),('owner..name@example.invalid',MISSING,None),
    ('.owner@example.invalid',MISSING,None),('owner@example..invalid',MISSING,None),
    ('x'*65+'@example.invalid',MISSING,None),('owner@'+'x'*64+'.invalid',MISSING,None),
    ('"owner"@example.invalid',MISSING,None),('owner@example.invalid/extra',MISSING,None),
    ('x'*64+'@'+'.'.join(['x'*63]*3)+'.invalid',MISSING,None),
])
def test_signed_email_policy_keeps_literal_uid(env,email,verified,expected):
    def mutate(claims):
        if email is not MISSING:claims['email']=email
        if verified is not MISSING:claims['email_verified']=verified
    who,_,_,_=attempt(env,mutate)
    assert who.email==expected and who.uid==UID


@pytest.mark.anyio
@pytest.mark.parametrize('name',['Тестовый участник','ASCII Name','e\u0301 / é',"O'Example <text> %2F + &",'Name 🧪','🧪'*256,''])
async def test_signed_name_survives_private_ascii_wire(tmp_path,name):
    platform=Platform();app=create_app(configuration(tmp_path),clients=platform,clock=lambda:1000);app.state.handoff_secret='h'*32
    async with browser_for(app) as browser:
        _,_,flow=await activate(browser,platform,name=name)
        response=await handoff(browser,flow)
        assert response.status_code==200
        assert response.headers['x-verified-name-utf8-b64']=='v1.'+base64.urlsafe_b64encode(name.encode()).decode().rstrip('=')
        assert response.headers['x-verified-uid']==UID and response.json()['displayName']==name
        assert 'x-verified-name' not in response.headers


@pytest.mark.parametrize('invalid',[None,123,'x'*257,'line\nbreak','\ud800'])
def test_invalid_signed_name_uses_safe_fallback(env,invalid):
    class EscapedJSON(json.JSONEncoder):
        def __init__(self,**kwargs):
            super().__init__(**{**kwargs,'ensure_ascii':True})
    assert attempt(env,lambda c:c.update(name=invalid),encoder=EscapedJSON)[0].name=='Участник Moodle'


@pytest.mark.parametrize('invalid',[None,123,'line\nbreak','\ud800','\udfff','🧪'*257])
def test_header_encoder_rejects_invalid_unicode_or_controls(invalid):
    with pytest.raises(LaunchError):verified_name_header(invalid)


@pytest.mark.anyio
async def test_other_browser_same_user_cannot_replace_saved_name_email_or_role(tmp_path):
    platform=Platform();app=create_app(configuration(tmp_path),clients=platform,clock=lambda:1000);app.state.handoff_secret='h'*32
    async with browser_for(app) as first,browser_for(app) as second:
        _,_,old=await activate(first,platform,name='Первое имя',email='old@example.invalid')
        await activate(second,platform,name='Новое имя',email='new@example.invalid')
        private=await handoff(first,old)
        assert private.json()['displayName']=='Первое имя'
        assert (await first.get(private.json()['target'])).status_code==303
        assert platform.calls==[(UID,'pilot','demo','student','old@example.invalid')]
