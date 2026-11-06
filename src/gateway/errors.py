class GatewayError(Exception):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status

class LaunchError(GatewayError):
    def __init__(self, message="Требуется подтверждённый переход из Moodle"):
        super().__init__(message, 403)
