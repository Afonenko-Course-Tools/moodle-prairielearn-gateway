"""Единственный закрытый контракт зачисления в PrairieLearn."""
import re
import httpx
from .errors import GatewayError
from .launch import normalize_email


class PLClient:
    def __init__(self, http, url, token):
        if len(token) < 32:
            raise ValueError('Private Bridge token должен содержать не менее 32 символов')
        self.http, self.url, self.token = http, url.rstrip('/'), token

    async def enroll(self, uid, instance, course, *, role, email):
        if role not in ('student', 'teacher') or email is not None and (not isinstance(email, str) or normalize_email(email) != email):
            raise GatewayError('Некорректная подтверждённая роль или email', 503)
        scope = {'userUid': uid, 'instance': instance, 'courseKey': course}
        try:
            response = await self.http.put(self.url + '/internal/gateway/enrollment',
                                           headers={'Authorization': 'Bearer ' + self.token},
                                           json={**scope, 'role': role, 'email': email}, follow_redirects=False, timeout=15)
            if response.status_code != 200 or len(response.content) > 65536:
                raise GatewayError('Отклонён ответ Bridge; повторите переход из Moodle', 503)
            answer = response.json()
            expected = {'schemaVersion': 1, **scope, 'enrolled': True}
            if (not isinstance(answer, dict) or set(answer) != set(expected) | {'path'}
                    or any(answer[k] != v or type(answer[k]) is not type(v) for k, v in expected.items())
                    or not isinstance(answer['path'], str)
                    or not re.fullmatch(r'/pl/course_instance/[1-9][0-9]*/assessments', answer['path'])):
                raise GatewayError('Ответ Bridge не подтверждает курс; повторите переход из Moodle', 503)
            return answer
        except (httpx.HTTPError, ValueError) as error:
            raise GatewayError('Bridge недоступен; повторите переход из Moodle', 503) from error
