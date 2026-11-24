"""Пассивная доставка подтверждённого учебного входа контроллеру ExamLab."""
import asyncio
from collections.abc import Collection
from http import HTTPStatus
import logging
from pathlib import Path
import re
import secrets
import ssl
import stat

import httpx
from starlette.datastructures import Headers

from .config import ExamAudit, ExamBinding
from .store import VerifiedLaunch


LOGGER = logging.getLogger(__name__)
WARNING = 'Доставка привязки ExamLab недоступна'
CERT_HEADER = 'x-exam-client-cert-id'
PROOF_HEADER = 'x-exam-client-cert-proof'
CERT_ID = re.compile(r'[a-f0-9]{64}:(?:[0-9A-F]{2}){1,20}')
TIMEOUT = 2.0


def private_token(path: str, other_tokens: Collection[str]) -> str:
    token_path = Path(path)
    info = token_path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise ValueError('Требуется закрытый файл токена привязки')
    token = token_path.read_text(encoding='ascii').strip()
    if not re.fullmatch(r'[a-f0-9]{64}', token) or any(secrets.compare_digest(token, other) for other in other_tokens):
        raise ValueError('Требуется отдельный токен привязки')
    return token


class BindingReporter:
    def __init__(self, config: ExamBinding, *, ca_file: str | None,
                 other_tokens: Collection[str], transport: httpx.AsyncBaseTransport | None = None) -> None:
        token = private_token(config.tokenFile, other_tokens)
        self.proof_token = private_token(config.proofTokenFile, (*other_tokens, token)).encode('ascii')
        self.endpoint = httpx.URL(config.controllerOrigin.rstrip('/') + '/exam/internal/lti-binding')
        self.http = httpx.AsyncClient(
            verify=ssl.create_default_context(cafile=ca_file), headers={'Authorization': 'Bearer ' + token},
            trust_env=False, follow_redirects=False, timeout=TIMEOUT, transport=transport)

    def certificate_id(self, headers: Headers) -> str | None:
        values = headers.getlist(CERT_HEADER)
        proofs = headers.getlist(PROOF_HEADER)
        if (len(values) != 1 or len(proofs) != 1 or not CERT_ID.fullmatch(values[0])
                or not secrets.compare_digest(proofs[0].encode(), self.proof_token)):
            return None
        return values[0]

    async def report(self, launch: VerifiedLaunch, exam: ExamAudit) -> None:
        cert_id = launch.bindingCertId
        if (launch.role != 'student' or launch.bindingExamId != exam.examId or launch.activity not in exam.activities
                or cert_id is None or not CERT_ID.fullmatch(cert_id)):
            return
        payload = {'examId': exam.examId, 'certId': cert_id, 'uid': launch.uid, 'student': launch.name}
        try:
            async with asyncio.timeout(TIMEOUT), self.http.stream('POST', self.endpoint, json=payload) as response:
                if response.status_code == HTTPStatus.NO_CONTENT:
                    return
        except (httpx.HTTPError, httpx.InvalidURL, TimeoutError):
            pass
        LOGGER.warning(WARNING)
