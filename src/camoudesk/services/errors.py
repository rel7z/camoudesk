"""Typed service errors. They subclass the built-ins the bridge envelope already maps, and the REST
API turns them into HTTP status codes (NotFound 404, Invalid 400, Conflict 409)."""


class NotFound(KeyError):
    def __str__(self) -> str:  # KeyError would quote the message
        return str(self.args[0]) if self.args else "Not found"


class Invalid(ValueError):
    pass


class Conflict(RuntimeError):
    pass
