class ITSMError(Exception):
    status_code: int = 500
    error_code: str = "INTERNAL_ERROR"
    message: str = "An unexpected error occurred"

    def __init__(self, message: str | None = None):
        if message is not None:
            self.message = message
        super().__init__(self.message)


class ValidationError(ITSMError):
    status_code = 400
    error_code = "VALIDATION_ERROR"
    message = "Validation failed"


class InvalidStateTransitionError(ITSMError):
    status_code = 409
    error_code = "INVALID_STATE_TRANSITION"

    def __init__(self, from_status: str, to_status: str, valid_transitions: list[str]):
        self.from_status = from_status
        self.to_status = to_status
        self.valid_transitions = valid_transitions
        self.message = f"Cannot transition from '{from_status}' to '{to_status}'"
        super(ITSMError, self).__init__(self.message)

    def extra_context(self) -> dict:
        return {
            "details": {
                "from_status": self.from_status,
                "to_status": self.to_status,
                "valid_transitions": self.valid_transitions,
            }
        }


class DuplicateIdempotencyKeyError(ITSMError):
    status_code = 200
    error_code = "IDEMPOTENT_REPLAY"
    message = "Duplicate request — returning original response"

    def __init__(self, cached_response: dict | None = None):
        self.cached_response = cached_response
        super(ITSMError, self).__init__(self.message)


class AuthenticationError(ITSMError):
    status_code = 401
    error_code = "AUTHENTICATION_REQUIRED"
    message = "Authentication is required"


class AuthorizationError(ITSMError):
    status_code = 403
    error_code = "INSUFFICIENT_PERMISSIONS"
    message = "You do not have permission to perform this action"


class TenantSuspendedError(ITSMError):
    status_code = 403
    error_code = "TENANT_SUSPENDED"
    message = "This tenant account has been suspended"


class SubscriptionRequiredError(ITSMError):
    status_code = 403
    error_code = "SUBSCRIPTION_REQUIRED"
    message = "An active subscription is required to access this feature"


class ResourceNotFoundError(ITSMError):
    status_code = 404
    error_code = "NOT_FOUND"

    def __init__(self, resource_type: str, resource_id: str):
        self.resource_type = resource_type
        self.resource_id = resource_id
        self.message = f"{resource_type} '{resource_id}' not found"
        super(ITSMError, self).__init__(self.message)

    def extra_context(self) -> dict:
        return {
            "details": {
                "resource_type": self.resource_type,
                "resource_id": self.resource_id,
            }
        }


class RateLimitError(ITSMError):
    status_code = 429
    error_code = "RATE_LIMIT_EXCEEDED"
    message = "Rate limit exceeded — please slow down"


class AIBudgetExhaustedError(ITSMError):
    status_code = 429
    error_code = "AI_BUDGET_EXHAUSTED"
    message = "Monthly AI token budget has been exhausted"


class ExternalServiceError(ITSMError):
    status_code = 503
    error_code = "EXTERNAL_SERVICE_UNAVAILABLE"

    def __init__(self, service_name: str):
        self.service_name = service_name
        self.message = f"External service '{service_name}' is currently unavailable"
        super(ITSMError, self).__init__(self.message)

    def extra_context(self) -> dict:
        return {"details": {"service": self.service_name}}
