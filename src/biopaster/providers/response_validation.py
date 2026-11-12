"""Classify invalid API data separately from valid but empty model replies."""
import json


class InvalidAPIResponseError(ValueError):
    def __init__(self, message, *, retryable=True):
        super().__init__(message)
        self.retryable = retryable


class StreamGuard:
    def __init__(self):
        self.output_started = False

    def check_headers(self, stream):
        response = getattr(stream, "response", None)
        content_type = getattr(response, "headers", {}).get("content-type", "").lower()
        if "html" in content_type:
            raise InvalidAPIResponseError(
                "API returned an HTML page instead of an API response; check the endpoint or proxy.",
                retryable=False,
            )

    def invalid(self, message):
        raise InvalidAPIResponseError(message, retryable=not self.output_started)

    def decoding_error(self, error):
        if isinstance(error, json.JSONDecodeError):
            if error.doc.lstrip().lower().startswith(("<!doctype html", "<html", "<head", "<body")):
                raise InvalidAPIResponseError("API returned HTML instead of JSON.", retryable=False) from error
            message = f"Invalid API JSON: {error.msg} at line {error.lineno}, column {error.colno}."
        else:
            message = "API response does not match the expected message schema."
        raise InvalidAPIResponseError(message, retryable=not self.output_started) from error

    async def aiter(self, stream, validation_error):
        iterator = aiter(stream)
        while True:
            try:
                event = await anext(iterator)
            except StopAsyncIteration:
                return
            except (json.JSONDecodeError, validation_error) as error:
                self.decoding_error(error)
            yield event

    def iter(self, stream, validation_error):
        iterator = iter(stream)
        while True:
            try:
                event = next(iterator)
            except StopIteration:
                return
            except (json.JSONDecodeError, validation_error) as error:
                self.decoding_error(error)
            yield event


def retry_invalid_response(error, attempt, max_retries, log):
    """One repair attempt; explicit no-retry callers retain that contract."""
    retry = attempt == 0 and error.retryable and max_retries != 0
    if retry and log is not None:
        log.record("invalid_api_response_retry", {"attempt": 1, "error": str(error)})
    return retry
