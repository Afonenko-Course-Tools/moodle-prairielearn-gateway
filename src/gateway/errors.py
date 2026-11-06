class GatewayError(Exception):
    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status

class LaunchError(GatewayError):
    def __init__(self, message: str = "Требуется подтверждённый переход из Moodle") -> None:
        super().__init__(message, 403)
