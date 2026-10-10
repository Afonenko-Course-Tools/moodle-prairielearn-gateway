class GatewayError(Exception):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status

class LaunchError(GatewayError):
    def __init__(self, message="Verified launch/session required"):
        super().__init__(message, 403)
